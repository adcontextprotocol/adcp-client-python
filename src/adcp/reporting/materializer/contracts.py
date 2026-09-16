"""B1 destination I/O contracts. No scheduling, persistence or capability claims.

Resolvers construct a session synchronously; authorization and resource acquisition
happen in its protected ``_open`` method, inside the SDK's owned async lifecycle.
Allocate credentials only there, retain them only on the redacted session, and
release partial acquisitions in ``_close``. Protocol callers supply opaque IDs.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Literal, Protocol, TypeVar

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import (
    ReportingCanonicalDigest,
    aware_utc,
    destination_reference,
    file_object_reference,
    native_version_reference,
    reporting_identifier,
    sha256_value,
)
from adcp.reporting.ledger.delivery_models import (
    DeliveryMethod,
    ReportingDeliveryPrincipal,
    ReportingDestinationBinding,
    ReportingFormat,
    ReportingMaterializationAttempt,
    ReportingObligationDeliveryRecord,
    ReportingResourceRecord,
    VerificationPath,
    VerificationProfile,
    _ClosedValue,
    _freeze_fields,
)
from adcp.reporting.ledger.models import (
    ReportingConfigurationGenerationKey,
    ReportingDefinitionBinding,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)

ReportingIOPhase = Literal["write", "readback"]
ReportingExternalEffect = Literal["not_started", "applied", "unknown"]
ReportingWriterRetry = Literal["never", "same_identity", "new_attempt"]
ReportingWriterFailureCode = Literal[
    "UNSUPPORTED_VERIFICATION",
    "HISTORY_CORRUPT",
    "REVISION_NOT_READY",
    "CURRENT_REVISION_CHANGED",
    "AUTHORIZATION_DENIED",
    "BINDING_MISMATCH",
    "SOURCE_INVALID",
    "DESTINATION_CORRUPT",
    "RESOURCE_UNAVAILABLE",
    "WRITE_FAILED",
    "DEADLINE_EXCEEDED",
    "LEASE_LOST",
    "LIMIT_EXCEEDED",
]


@dataclass(frozen=True, slots=True)
class ReportingWriterFailure(_ClosedValue):
    """Safe diagnostics only. Unknown effects must retain the original identity.

    ``new_attempt`` is usable by B2 only for its own known terminal failure.
    Public ledger persistence still allows N+1 after any immutable outcome.
    Consumer receipt rejection is outside this contract.
    """

    code: ReportingWriterFailureCode
    retry: ReportingWriterRetry = "never"
    effect: ReportingExternalEffect = "not_started"
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        _freeze_fields(self)
        if self.effect == "unknown" and self.retry == "new_attempt":
            raise ValueError("unknown external effects require the original identity")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry delay must be nonnegative")


class ReportingWriterError(Exception):
    """A closed failure; never pass provider prose or attach a provider cause."""

    def __init__(self, failure: ReportingWriterFailure) -> None:
        if type(failure) is not ReportingWriterFailure:
            raise TypeError("writer errors require a closed failure")
        self.failure = failure
        super().__init__(failure.code)

    def __repr__(self) -> str:
        return f"ReportingWriterError({self.failure.code})"


def failure(code: ReportingWriterFailureCode) -> ReportingWriterError:
    return ReportingWriterError(ReportingWriterFailure(code))


@dataclass(frozen=True, slots=True)
class ReportingWriterCapability(_ClosedValue):
    method: DeliveryMethod
    transport: str
    format: ReportingFormat | None
    verification_profile: VerificationProfile
    verification_path: VerificationPath
    immutability: Literal["immutable_location", "native_version"]
    checksum: Literal["sha256"]
    write_semantics: Literal["conditional_create", "idempotent"]

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.transport, maximum=64)
        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.transport) is None:
            raise ValueError("transport requires a public protocol label")
        if self.method == "file_transfer" and self.format is None:
            raise ValueError("file transfer requires a format")
        if self.verification_profile == "manifest_checksums" and self.method != "file_transfer":
            raise ValueError("manifest verification requires file transfer")
        if (
            (self.method == "dataset_share" and self.verification_path != "representative_consumer")
            or (
                self.method == "warehouse_materialization"
                and self.verification_path != "destination"
            )
            or (
                self.verification_profile == "native_commit"
                and self.immutability != "native_version"
            )
            or (self.immutability == "native_version" and self.verification_path == "producer")
        ):
            raise ValueError("capability requires its exact immutable observation path")


@dataclass(frozen=True, slots=True)
class ReportingCanonicalization(_ClosedValue):
    canonicalization_id: str
    canonicalization_uri: str
    canonicalization_sha256: str

    def __post_init__(self) -> None:
        _freeze_fields(self)
        ReportingCanonicalDigest(
            "0" * 64,
            self.canonicalization_id,
            self.canonicalization_uri,
            self.canonicalization_sha256,
        )
        object.__setattr__(self, "canonicalization_sha256", self.canonicalization_sha256.lower())


@dataclass(frozen=True, slots=True)
class ReportingVerificationKey(_ClosedValue):
    """Complete frozen identity, including method/format/profile/path and schema."""

    report_definition_id: str
    reporting_profile: str
    definition: ReportingDefinitionBinding
    canonicalization: ReportingCanonicalization
    capability: ReportingWriterCapability

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.report_definition_id)
        reporting_identifier(self.reporting_profile, maximum=128)
        for value in (self.definition.report_definition_sha256, self.definition.schema_sha256):
            if type(value) is not str:
                raise ValueError("definition digests require exact strings")
            sha256_value(value)
        for uri in (self.definition.report_definition_uri, self.definition.schema_uri):
            # Reuse the strict public HTTPS contract screen; never dereference it.
            ReportingCanonicalDigest("0" * 64, "definition", uri, "0" * 64)
        for value in (self.definition.schema_version, self.definition.schema_ref_policy):
            reporting_identifier(value, maximum=128)
        ReportingCanonicalDigest("0" * 64, "dialect", self.definition.schema_dialect, "0" * 64)
        for units in (
            self.definition.monetary_metric_units,
            self.definition.monetary_control_total_units,
        ):
            for name, unit in units:
                reporting_identifier(name, maximum=128)
                reporting_identifier(unit, maximum=32)
        object.__setattr__(
            self,
            "definition",
            replace(
                self.definition,
                report_definition_sha256=self.definition.report_definition_sha256.lower(),
                schema_sha256=self.definition.schema_sha256.lower(),
            ),
        )


def binding_fingerprint(binding: ReportingDestinationBinding) -> str:
    value = asdict(binding)
    value["created_at"] = binding.created_at.isoformat()
    return hashlib.sha256(canonical_json_utf8_v1(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ReportingDestinationRequest(_ClosedValue):
    """Trusted resolver input. Aliases must already resolve to the canonical consumer."""

    principal: ReportingDeliveryPrincipal
    generation: ReportingConfigurationGenerationKey
    destination_ref: str
    trusted_binding_ref: str = field(repr=False)
    binding_fingerprint: str
    verification_key: ReportingVerificationKey
    reporting_obligation_id: str
    reporting_revision_id: str
    reporting_materialization_id: str
    attempt: int

    def __post_init__(self) -> None:
        _freeze_fields(self)
        if self.principal.account_id != self.generation.account_id or self.attempt < 1:
            raise ValueError("destination request requires an exact account and attempt")
        reporting_identifier(self.generation.delivery_config_id)
        if (
            type(self.generation.delivery_config_version) is not int
            or self.generation.delivery_config_version < 1
        ):
            raise ValueError("destination request requires an exact generation")
        destination_reference(self.destination_ref)
        destination_reference(self.trusted_binding_ref)
        sha256_value(self.binding_fingerprint)
        object.__setattr__(self, "binding_fingerprint", self.binding_fingerprint.lower())
        for value in (
            self.reporting_obligation_id,
            self.reporting_revision_id,
            self.reporting_materialization_id,
        ):
            reporting_identifier(value)

    @classmethod
    def from_binding(
        cls,
        binding: ReportingDestinationBinding,
        attempt: ReportingMaterializationAttempt,
        key: ReportingVerificationKey,
    ) -> ReportingDestinationRequest:
        cap = key.capability
        if (
            binding.principal != attempt.scope.principal
            or binding.generation_key != attempt.scope.generation_key
            or (binding.method, binding.transport, binding.format, binding.verification_profile)
            != (cap.method, cap.transport, cap.format, cap.verification_profile)
            or (binding.success_status == "delivered" and cap.verification_path != "destination")
        ):
            raise failure("BINDING_MISMATCH")
        return cls(
            binding.principal,
            binding.generation_key,
            binding.destination_ref,
            binding.trusted_binding_ref,
            binding_fingerprint(binding),
            key,
            attempt.scope.reporting_obligation_id,
            attempt.reporting_revision_id,
            attempt.reporting_materialization_id,
            attempt.attempt,
        )

    @property
    def external_id(self) -> str:
        """Stable across pending retries; isolated across tenants and revision attempts."""
        return "rwm_" + hashlib.sha256(canonical_json_utf8_v1(asdict(self))).hexdigest()


@dataclass(frozen=True, slots=True)
class ReportingDestinationLocator(_ClosedValue):
    """Writer claims identify what to read. They are never verification proof."""

    external_id: str
    binding_fingerprint: str
    resource: ReportingResourceRecord

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.external_id)
        sha256_value(self.binding_fingerprint)
        object.__setattr__(self, "binding_fingerprint", self.binding_fingerprint.lower())


@dataclass(frozen=True, slots=True)
class ReportingPreparedRevision(_ClosedValue):
    """Bounded immutable canonical row bytes, produced before resolver I/O.

    This is not a durable reservation. Keep the original attempt identity when
    an external effect is unknown. Never automatically import legacy pending
    attempts: drain old writers and recover/import their identities explicitly.
    """

    request: ReportingDestinationRequest
    obligation: ReportingObligationRecord = field(repr=False)
    revision: ReportingRevisionRecord = field(repr=False)
    delivery: ReportingObligationDeliveryRecord = field(repr=False)
    binding: ReportingDestinationBinding = field(repr=False)
    rows: tuple[bytes, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _freeze_fields(self)


@dataclass(frozen=True, slots=True)
class ReportingDestinationPage(_ClosedValue):
    reporting_revision_id: str
    rows: tuple[bytes, ...] = field(repr=False)
    total_count: int
    has_more: bool
    cursor: str | None
    format: ReportingFormat | None
    verification_path: VerificationPath
    native_version_ref: str | None = None

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_revision_id)
        if self.total_count < 0 or self.has_more != (self.cursor is not None):
            raise ValueError("destination page requires a paired cursor and valid total")
        if self.cursor is not None:
            reporting_identifier(self.cursor, maximum=2048)
        if self.native_version_ref is not None:
            native_version_reference(self.native_version_ref)


@dataclass(frozen=True, slots=True)
class ReportingNativeObservation(_ClosedValue):
    location: str
    native_version_ref: str
    verification_path: Literal["representative_consumer", "destination"]

    def __post_init__(self) -> None:
        _freeze_fields(self)
        from adcp.reporting.evidence import resource_location

        resource_location(self.location)
        native_version_reference(self.native_version_ref)


def object_path(value: str) -> str:
    file_object_reference(value)
    if "%" in value or ":" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("object references require decoded relative path components")
    return value


class ReportingHeartbeat(Protocol):
    """B2 owns any parallel lease heartbeat. B1 only calls this checkpoint."""

    async def checkpoint(self) -> None: ...


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ReportingIOContext:
    deadline_at: datetime
    cancel: asyncio.Event = field(repr=False)
    heartbeat: ReportingHeartbeat | None = field(default=None, repr=False)
    close_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "deadline_at", aware_utc(self.deadline_at))
        if not math.isfinite(self.close_timeout_seconds) or self.close_timeout_seconds <= 0:
            raise ValueError("close timeout must be finite and positive")

    async def run(
        self, call: Callable[[], Awaitable[T]], *, effect: ReportingExternalEffect = "not_started"
    ) -> T:
        if self.heartbeat is not None:
            await self._run(self.heartbeat.checkpoint, effect=effect)
        return await self._run(call, effect=effect)

    async def _run(self, call: Callable[[], Awaitable[T]], *, effect: ReportingExternalEffect) -> T:
        if self.cancel.is_set():
            raise asyncio.CancelledError
        remaining = (self.deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise ReportingWriterError(
                ReportingWriterFailure("DEADLINE_EXCEEDED", "same_identity", effect)
            )

        async def invoke() -> T:
            return await call()

        task = asyncio.create_task(invoke())
        canceled = asyncio.create_task(self.cancel.wait())
        problem: ReportingWriterFailure | None = None
        was_canceled = False
        try:
            done, _ = await asyncio.wait(
                (task, canceled), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if canceled in done:
                was_canceled = True
            elif task not in done:
                problem = ReportingWriterFailure("DEADLINE_EXCEEDED", "same_identity", effect)
            else:
                try:
                    result = task.result()
                except ReportingWriterError as exc:
                    problem = exc.failure
                    if (
                        effect == "unknown"
                        and problem.effect == "not_started"
                        and problem.code
                        in {"RESOURCE_UNAVAILABLE", "DEADLINE_EXCEEDED", "LEASE_LOST"}
                    ):
                        problem = replace(problem, effect="unknown", retry="same_identity")
                except Exception:
                    problem = ReportingWriterFailure(
                        "RESOURCE_UNAVAILABLE", "same_identity", effect
                    )
        except asyncio.CancelledError:
            was_canceled = True
        finally:
            task.cancel()
            canceled.cancel()
            if await _join_tasks(task, canceled):
                was_canceled = True
        if was_canceled:
            raise asyncio.CancelledError
        # Outside the except suite: no provider __context__, even when inspected.
        if problem is not None:
            raise ReportingWriterError(problem)
        return result


async def _join_tasks(*tasks: asyncio.Task[Any]) -> bool:
    """Finish cancellation cleanup even if the caller is canceled repeatedly."""
    joined = asyncio.gather(*tasks, return_exceptions=True)
    canceled = False
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            canceled = True
    joined.result()
    return canceled


class ReportingDestinationSession(ABC):
    """Single-use SDK-owned lifecycle around an adopter's private provider session.

    Override only _open/_close and I/O methods. _close must tolerate partial
    _open, finish promptly, and remove any adopter-owned temporary spool. The
    SDK invokes it exactly once, shields cancellation, and joins all its tasks.
    No credentials may be placed on the public request, descriptor or results.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        owned = {
            "__repr__",
            "__str__",
            "__reduce__",
            "__reduce_ex__",
            "__getstate__",
            "__aenter__",
            "__aexit__",
            "aclose",
            "request",
            "phase",
            "context",
        }
        if owned.intersection(cls.__dict__):
            raise TypeError("destination session lifecycle and redaction belong to the SDK")

    def __init__(
        self,
        request: ReportingDestinationRequest,
        phase: ReportingIOPhase,
        context: ReportingIOContext,
    ) -> None:
        if (
            type(request) is not ReportingDestinationRequest
            or phase not in ("write", "readback")
            or type(context) is not ReportingIOContext
        ):
            raise failure("BINDING_MISMATCH")
        self._request, self._phase, self._context = request, phase, context
        self._entered = False
        self._closed = False

    def __repr__(self) -> str:
        return "<ReportingDestinationSession redacted>"

    __str__ = __repr__

    @property
    def request(self) -> ReportingDestinationRequest:
        return self._request

    @property
    def phase(self) -> ReportingIOPhase:
        return self._phase

    @property
    def context(self) -> ReportingIOContext:
        return self._context

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError("destination sessions cannot be persisted")

    @abstractmethod
    async def _open(self) -> None: ...

    @abstractmethod
    async def _close(self) -> None: ...

    async def __aenter__(self) -> ReportingDestinationSession:
        if self._entered or self._closed:
            raise failure("BINDING_MISMATCH")
        self._entered = True
        problem: ReportingWriterFailure | None = None
        canceled = False
        try:
            await self.context.run(self._open)
        except asyncio.CancelledError:
            canceled = True
        except ReportingWriterError as exc:
            problem = exc.failure
        if canceled or problem is not None:
            try:
                await self.aclose()
            except asyncio.CancelledError:
                canceled = True
            except ReportingWriterError:
                pass
            if canceled or self.context.cancel.is_set():
                raise asyncio.CancelledError
            assert problem is not None
            raise ReportingWriterError(problem)
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.aclose()
        except ReportingWriterError:
            if kind is None:
                raise
        if self.context.cancel.is_set():
            raise asyncio.CancelledError
        if kind is None and datetime.now(timezone.utc) >= self.context.deadline_at:
            raise ReportingWriterError(
                ReportingWriterFailure("DEADLINE_EXCEEDED", "same_identity", "unknown")
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_owned(self._close, self.context.close_timeout_seconds)

    async def write(self, content: ReportingPreparedRevision) -> ReportingDestinationLocator:
        raise failure("UNSUPPORTED_VERIFICATION")

    async def read_rows(
        self, locator: ReportingDestinationLocator, *, cursor: str | None, limit: int
    ) -> ReportingDestinationPage:
        raise failure("UNSUPPORTED_VERIFICATION")

    async def read_manifest(self, locator: ReportingDestinationLocator) -> bytes:
        raise failure("UNSUPPORTED_VERIFICATION")

    async def list_objects(self, locator: ReportingDestinationLocator) -> tuple[str, ...]:
        raise failure("UNSUPPORTED_VERIFICATION")

    def read_object(
        self, locator: ReportingDestinationLocator, *, object_ref: str
    ) -> AsyncIterator[bytes]:
        raise failure("UNSUPPORTED_VERIFICATION")

    async def observe_native_version(
        self, locator: ReportingDestinationLocator
    ) -> ReportingNativeObservation:
        raise failure("UNSUPPORTED_VERIFICATION")


class ReportingDestinationResolver(Protocol):
    """Return an unopened session. _open reauthorizes each phase independently.

    Resolution must match every request coordinate, including consumer URL,
    immutable trusted binding, definition/canonicalization and capability. A
    resolver must never allocate resources before constructing the session.
    """

    def resolve(
        self,
        request: ReportingDestinationRequest,
        *,
        phase: ReportingIOPhase,
        context: ReportingIOContext,
    ) -> ReportingDestinationSession: ...


class ReportingDestinationWriter(Protocol):
    @property
    def capabilities(self) -> tuple[ReportingWriterCapability, ...]: ...

    @property
    def production_eligible(self) -> bool: ...


async def _close_owned(close: Callable[[], Awaitable[None]], timeout: float) -> None:
    """Cancellation-safe joining shared by the session and object stream lifecycles."""

    async def invoke() -> None:
        await close()

    task = asyncio.create_task(asyncio.wait_for(invoke(), timeout))
    canceled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            canceled = True
        except Exception:
            break
    if canceled or task.cancelled():
        if not task.cancelled():
            task.exception()  # Retrieve any failure while propagating a clean cancellation.
        raise asyncio.CancelledError
    if task.exception() is not None:
        raise ReportingWriterError(
            ReportingWriterFailure("RESOURCE_UNAVAILABLE", "same_identity", "unknown")
        )
