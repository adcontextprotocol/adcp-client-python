"""Wrap an existing delivery fetch into a conforming reporting source executor.

Most sellers adopting Reliable Reporting already have the numbers.  They have a
``get_media_buy_delivery`` handler, or a nightly stats cache, or a report-API
client someone wrote years ago.  What they do not have is an immutable,
replayable, evidence-bearing publication -- and hand-rolling one means getting
eight interlocking fingerprints, a zero-row taxonomy, and a replay seal right
on the first try.

:class:`InlineReportingSource` is that adapter.  Give it a callable that
answers "what did this scope deliver in this window?" and it produces a
conforming ``basic`` manifest, complete with staged objects, coverage
evidence, and byte-identical replay.

The three answers a fetch can give
---------------------------------

``None``
    **Not ready.**  The source does not have this period yet.  This is not a
    zero and it is not a failure; it is "ask again later".  For a
    ``full``-coverage request that means a retryable ``PARTIAL_RESULT`` and no
    publication.  For a ``partial``-coverage request it means a completed
    publication whose cells are all ``delayed`` -- which is *useful*, because
    the producer's health projection can then distinguish "late" from "dead".

``[]`` (an empty row sequence)
    **A real zero-row period.**  The source answered for the whole requested
    scope and the answer was nothing.  That is data: it commits an
    ``explicit_zero`` revision and satisfies the obligation.  Conflating this
    with ``None`` is the single most common Reliable Reporting bug, because it
    makes a dead feed look like a quiet campaign forever.

an exception
    **A failure.**  Raise :class:`~adcp.reporting.source.ReportingSourceError`
    to classify it yourself.  Anything else is treated as an unclassified
    transient fault (see :attr:`InlineReportingSource.include_exception_detail`
    for why the message is redacted by default).

Sync callables
--------------

A synchronous fetch runs in a worker thread.  Python cannot interrupt a
running thread, so a sync fetch is **not cancellable**: on cancellation this
executor waits for the thread rather than returning while work continues
behind it, because a detached fetch would race the producer's next attempt for
the same staged object names.  Give your sync callable its own timeout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

from adcp.reporting.currency import (
    ReportingCurrencyError,
    validate_currency,
    validate_row_currencies,
)
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    PackageItemConstituentV1,
    ProductConstituentV1,
    ReportingAvailabilityStatus,
    ReportingConstituentCoverageV1,
    ReportingFinalityEvidenceV1,
    ReportingMetricAvailabilityV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceError,
    ReportingSourceErrorV1,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
    SourceBatchCoverageV1,
    SourceBatchManifestReferenceV1,
    SourceBatchManifestV1,
    SourceBatchObjectV1,
    SourceControlTotalV1,
    deterministic_source_publication_id_v1,
    encode_source_batch_manifest_v1,
    publication_content_fingerprint_v1,
    source_batch_manifest_reference_v1,
    source_batch_object_set_sha256_v1,
)

__all__ = [
    "FileSystemStagingStore",
    "InMemorySealStore",
    "InMemoryStagingStore",
    "InlineFetch",
    "InlineFetchResult",
    "InlineReportingSource",
    "ReportingSealStore",
    "ReportingStagingStore",
    "SealedSlice",
]


# --------------------------------------------------------------------------
# Fetch results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InlineFetchResult:
    """A fetch answer richer than a bare row list.

    Return this when the honest answer is nuanced: the cache covers some
    constituents but not others, the numbers are only good through a
    freshness watermark earlier than the period end, or a particular scope is
    known-unsupported rather than known-empty.
    """

    rows: Sequence[Mapping[str, Any]]
    """Normalized rows.  Empty means an observed zero for every covered scope."""

    data_through: datetime | None = None
    """How far into the period these numbers are actually good.

    This is the freshness gate.  An ad server that finalizes yesterday at
    07:00 local is not telling you about the last four hours at 05:00, and
    publishing those hours as complete is how an official close ends up
    understating delivery.  Defaults to ``min(period end, source read
    cutoff)``; supply the real watermark when you know it.
    """

    covered_constituent_ids: Collection[str] | None = None
    """Which constituents this answer actually covers.

    ``None`` (the default) means "all of them" -- an answer for the whole
    requested scope, so a constituent with no rows is an observed zero.  Name
    a subset when your cache genuinely only holds part of the denominator;
    everything outside it is reported ``missing`` rather than zero.
    """

    unavailable_constituents: Mapping[str, str] = field(default_factory=dict)
    """``constituent_id`` to a stable reason, for scopes this source cannot serve.

    Use for known-unsupported scopes (a media buy on a different ad server, a
    package the report definition does not model).  Distinct from "covered but
    empty", which is a zero.
    """

    unavailable_status: ReportingAvailabilityStatus = "unsupported"
    """The status to attach to :attr:`unavailable_constituents`."""

    warnings: Sequence[str] = ()
    """Safe, redacted operator notes retained on the publication."""

    currency: str | None = None
    """Optional source corroboration; must match the already frozen request."""

    provisional_until: datetime | None = None
    """Source evidence that this snapshot may still change until this instant.

    When present it overrides the offering's default restatement window for
    this slice. It is retained in the manifest so a durable producer can keep
    scheduling correctly after a restart.
    """


#: What an inline fetch may return.  ``None`` is "not ready".
InlineFetchReturn: TypeAlias = "InlineFetchResult | Sequence[Mapping[str, Any]] | None | object"

#: The callable shape.  Sync or async; a sync callable runs in a worker thread.
InlineFetch: TypeAlias = "Callable[[ReportingSourceSliceRequestV1], Any]"


# --------------------------------------------------------------------------
# Staging and sealing
# --------------------------------------------------------------------------


@runtime_checkable
class ReportingStagingStore(Protocol):
    """Where normalized rows live, immutably, for as long as the manifest does.

    ``stage`` returns an ``(object_ref, object_generation)`` pair.  The pair,
    not the ref alone, is the immutable identity: an object-store key can be
    overwritten, a key plus generation cannot.  Re-reading the pair must either
    return the same bytes or fail -- never different bytes.
    """

    async def stage(
        self, *, account_id: str, source_execution_key: str, ordinal: int, payload: bytes
    ) -> tuple[str, str]: ...

    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Mapping[str, Any],
        cancel: asyncio.Event,
    ) -> bytes: ...


@dataclass(frozen=True)
class SealedSlice:
    """The immutable result of one execution, kept for replay."""

    reference: SourceBatchManifestReferenceV1
    manifest_bytes: bytes


@runtime_checkable
class ReportingSealStore(Protocol):
    """Remembers what a ``source_execution_key`` already published.

    This is what makes replay identity possible at all.  A manifest carries
    real observation and acquisition times; recomputing them on a second
    execution would produce different bytes for the same logical work.  Sealing
    the first result and returning it verbatim is the only correct answer, and
    it is also what cancellation semantics require: a cancelled execution
    publishes nothing *unless the exact idempotent result was already sealed*.
    """

    async def get(self, *, account_id: str, source_execution_key: str) -> SealedSlice | None: ...

    async def put(
        self, *, account_id: str, source_execution_key: str, sealed: SealedSlice
    ) -> SealedSlice:
        """Persist the seal, returning whichever seal won a concurrent race.

        Two workers may execute the same key at once.  The store decides the
        winner; both callers then return the winning bytes, so replay identity
        holds even under a double dispatch.
        """
        ...


class InMemoryStagingStore:
    """Process-local staging.  Fine for tests and single-process pilots.

    Not durable: a restart loses the rows a retained manifest still points at,
    which breaks exact revision reads. Use :class:`FileSystemStagingStore` or
    your own object store in production.
    """

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self._scopes: dict[tuple[str, str], str] = {}

    async def stage(
        self, *, account_id: str, source_execution_key: str, ordinal: int, payload: bytes
    ) -> tuple[str, str]:
        digest = hashlib.sha256(payload).hexdigest()
        object_ref = f"{source_execution_key}.{ordinal}"
        self._objects[(object_ref, digest)] = payload
        self._scopes[(object_ref, digest)] = account_id
        return object_ref, digest

    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Mapping[str, Any],
        cancel: asyncio.Event,
    ) -> bytes:
        key = (object_ref, object_generation)
        if self._scopes.get(key) != account_id:
            raise PermissionError("staged object is outside the requested account scope")
        return self._objects[key]


class FileSystemStagingStore:
    """Content-addressed staging on a filesystem.

    ``object_generation`` is the SHA-256 of the bytes and the file is named for
    it, so the store is immutable by construction: staging identical bytes
    twice is a no-op, and staging different bytes produces a different
    generation rather than overwriting the first.  Writes land via
    temp-file-and-rename so a crashed write never leaves a half-file a manifest
    already points at.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, account_id: str, object_ref: str, object_generation: str) -> Path:
        # The account is part of the path so an authorization bug cannot become
        # a cross-tenant read by way of a guessed object name.
        account = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:32]
        safe_ref = hashlib.sha256(object_ref.encode("utf-8")).hexdigest()[:32]
        return self._root / account / safe_ref / f"{object_generation}.bin"

    async def stage(
        self, *, account_id: str, source_execution_key: str, ordinal: int, payload: bytes
    ) -> tuple[str, str]:
        digest = hashlib.sha256(payload).hexdigest()
        object_ref = f"{source_execution_key}.{ordinal}"
        target = self._path(account_id, object_ref, digest)
        await asyncio.to_thread(self._write, target, payload)
        return object_ref, digest

    @staticmethod
    def _write(target: Path, payload: bytes) -> None:
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(target.parent))
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Mapping[str, Any],
        cancel: asyncio.Event,
    ) -> bytes:
        target = self._path(account_id, object_ref, object_generation)
        payload: bytes = await asyncio.to_thread(target.read_bytes)
        if hashlib.sha256(payload).hexdigest() != object_generation:
            raise OSError("staged object bytes no longer match their pinned generation")
        return payload


class InMemorySealStore:
    """Process-local replay seals.  Durable stores belong in your database."""

    def __init__(self) -> None:
        self._seals: dict[tuple[str, str], SealedSlice] = {}

    async def get(self, *, account_id: str, source_execution_key: str) -> SealedSlice | None:
        return self._seals.get((account_id, source_execution_key))

    async def put(
        self, *, account_id: str, source_execution_key: str, sealed: SealedSlice
    ) -> SealedSlice:
        return self._seals.setdefault((account_id, source_execution_key), sealed)


# --------------------------------------------------------------------------
# The executor
# --------------------------------------------------------------------------


class InlineReportingSource:
    """A conforming executor over an ordinary delivery fetch.

    Parameters
    ----------
    capabilities:
        Your truthful, scoped capability declaration.  Every offering it
        declares must use ``manifest_strictness='basic'`` -- this adapter
        cannot honestly produce per-call provider evidence for a fetch it did
        not make itself.
    fetch:
        Sync or async callable taking the frozen
        :class:`~adcp.reporting.source.ReportingSourceSliceRequestV1` and
        returning rows, an :class:`InlineFetchResult`, an AdCP
        ``GetMediaBuyDeliveryResponse``, or ``None`` for "not ready".
    staging / seals:
        Where rows and replay seals live.  The in-memory defaults are for tests
        and single-process pilots; they are not durable.
    constituent_of:
        Maps a row to the ``constituent_id`` it belongs to.  The default
        matches a row's ``media_buy_id`` / ``package_id`` / ``product_id``
        against the requested denominator, which covers the common shapes.
        Rows that match nothing are retained in the staged object but excluded
        from coverage, and a warning names how many were dropped.
    include_exception_detail:
        Whether an unclassified exception's ``str()`` may enter the retained
        safe message.  **Off by default**: HTTP client exceptions routinely
        stringify the full request URL, and a signed URL or an ``access_token``
        query parameter in a manifest is a credential leak into permanent,
        replicated evidence.  The exception *type* is always recorded.
    clock:
        Supplies the observation instant.  Override it to make a test's
        publications deterministic; the default is the wall clock, truncated
        to the millisecond precision the manifest binds.
    """

    def __init__(
        self,
        *,
        capabilities: ReportingSourceCapabilitiesV1,
        fetch: InlineFetch,
        staging: ReportingStagingStore | None = None,
        seals: ReportingSealStore | None = None,
        constituent_of: (
            Callable[[Mapping[str, Any], ReportingSourceSliceRequestV1], str | None] | None
        ) = None,
        staged_commit_prefix: str = "inline",
        include_exception_detail: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        unsupported = [
            offering.offering_id
            for offering in capabilities.offerings
            if offering.manifest_strictness != "basic"
        ]
        if unsupported:
            raise ValueError(
                "InlineReportingSource emits basic manifests only; offerings "
                f"{sorted(unsupported)} declare evidenced manifests, which would require "
                "per-call provider evidence this adapter cannot truthfully produce"
            )
        self._capabilities = capabilities
        self._fetch = fetch
        self._staging = staging or InMemoryStagingStore()
        self._seals = seals or InMemorySealStore()
        self._constituent_of = constituent_of or _default_constituent_of
        self._staged_commit_prefix = staged_commit_prefix
        self.include_exception_detail = include_exception_detail
        self._clock = clock or _now

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self._capabilities

    @property
    def staging(self) -> ReportingStagingStore:
        """The staging store, which also serves as the staged-object reader."""
        return self._staging

    async def execute(
        self,
        request: ReportingSourceSliceRequestV1,
        *,
        cancel: asyncio.Event,
        heartbeat: Callable[[], None] | None = None,
    ) -> ReportingSourceExecutorResult:
        account_id = request.identity.account_id
        key = request.identity.source_execution_key

        sealed = await self._seals.get(account_id=account_id, source_execution_key=key)
        if sealed is not None:
            # Replay, including replay after cancellation: the publication is
            # already immutable, so returning anything else would be a lie.
            return ReportingSourceExecutorResult.completed(
                request=request,
                manifest=sealed.reference,
                manifest_bytes=sealed.manifest_bytes,
            )

        if cancel.is_set():
            return ReportingSourceExecutorResult.failed(
                ReportingSourceErrorV1(
                    code="CANCELLED",
                    retry="cancelled",
                    safe_message="the slice was cancelled before any source read began",
                )
            )

        try:
            answer = await self._invoke(request, heartbeat)
            if answer is not None:
                # Coerce inside the guard: a fetch that returns something
                # unrecognizable is a source failure like any other, not an
                # exception escaping the executor.
                answer = _coerce(answer, currency=request.currency)
                # Retain the rows we checked across staging awaits. A fetch
                # may return a shared cache whose mappings later change.
                answer = replace(answer, rows=tuple(deepcopy(dict(row)) for row in answer.rows))
                if (
                    answer.currency is not None
                    and validate_currency(answer.currency) != request.currency
                ):
                    raise ReportingCurrencyError(
                        "CURRENCY_MISMATCH",
                        "inline source currency disagrees with the frozen request",
                    )
                # Before staging and, crucially, before _control_totals aggregates.
                validate_row_currencies(request.currency, answer.rows)
        except ReportingCurrencyError as error:
            return ReportingSourceExecutorResult.failed(
                ReportingSourceErrorV1(
                    code="INTEGRITY_FAILED", retry="terminal", safe_message=str(error)
                )
            )
        except ReportingSourceError as error:
            return ReportingSourceExecutorResult.failed(error.error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return ReportingSourceExecutorResult.failed(self._unclassified(error))

        if answer is None:
            not_ready = self._not_ready(request)
            if not_ready is not None:
                return not_ready
            answer = InlineFetchResult(
                rows=[],
                covered_constituent_ids=(),
                unavailable_constituents={
                    item.constituent_id: "Source has not produced this period yet"
                    for item in request.coverage.constituents
                },
                unavailable_status="delayed",
            )

        return await self._publish(request, answer, cancel=cancel)

    # -- fetch ----------------------------------------------------------

    async def _invoke(
        self, request: ReportingSourceSliceRequestV1, heartbeat: Callable[[], None] | None
    ) -> Any:
        """Call the fetch, keeping a synchronous one off the event loop.

        A blocking report API call on the loop thread stalls every other slice
        the producer is running, so a sync callable goes to a worker thread.
        The thread is then awaited to completion even under cancellation:
        Python cannot interrupt it, and returning while it still runs would
        leave it racing the producer's next attempt for the same staged object
        names.
        """
        if heartbeat is not None:
            heartbeat()
        answer: Any
        if _is_async_callable(self._fetch):
            answer = await self._fetch(request)
        else:
            answer = await asyncio.shield(asyncio.to_thread(self._fetch, request))
        if isinstance(answer, Awaitable):
            # A plain callable that hands back a coroutine; await it here
            # rather than letting it reach the coercion step unfinished.
            answer = await answer
        return answer  # narrowed by _coerce

    def _unclassified(self, error: Exception) -> ReportingSourceErrorV1:
        """Classify an exception nobody classified for us.

        Retryable, deliberately.  A transient network blip that we call
        terminal permanently fails a period until a human replays it; a
        genuinely broken source that we call retryable still escalates on its
        own when the producer's recovery window elapses.  The second failure
        mode is strictly cheaper.
        """
        detail = f": {error}" if self.include_exception_detail else ""
        return ReportingSourceErrorV1(
            code="PROVIDER_TRANSIENT",
            retry="retryable",
            safe_message=f"the reporting source raised {type(error).__name__}{detail}"[:1_024],
        )

    def _not_ready(
        self, request: ReportingSourceSliceRequestV1
    ) -> ReportingSourceExecutorResult | None:
        if request.coverage.expected != "full":
            return None
        return ReportingSourceExecutorResult.failed(
            ReportingSourceErrorV1(
                code="PARTIAL_RESULT",
                retry="retryable",
                safe_message=(
                    "the reporting source has not produced this period yet; a full-coverage "
                    "slice cannot complete partially"
                ),
            )
        )

    # -- publication ----------------------------------------------------

    async def _publish(
        self,
        request: ReportingSourceSliceRequestV1,
        result: InlineFetchResult,
        *,
        cancel: asyncio.Event,
    ) -> ReportingSourceExecutorResult:
        observed_at = self._clock()
        data_through = _clamp_watermark(request, result.data_through, observed_at)
        covered = (
            {item.constituent_id for item in request.coverage.constituents}
            if result.covered_constituent_ids is None
            else set(result.covered_constituent_ids)
        )
        covered -= set(result.unavailable_constituents)

        rows_by_constituent: dict[str, list[Mapping[str, Any]]] = {
            constituent_id: [] for constituent_id in covered
        }
        unmatched = 0
        for row in result.rows:
            constituent_id = self._constituent_of(row, request)
            if constituent_id is None or constituent_id not in rows_by_constituent:
                unmatched += 1
                continue
            rows_by_constituent[constituent_id].append(row)

        warnings = list(result.warnings)
        if unmatched:
            warnings.append(
                f"{unmatched} source row(s) matched no requested constituent and are "
                "retained in staging but excluded from coverage"
            )

        statuses = self._derive_statuses(
            request, result, covered, rows_by_constituent, data_through
        )
        coverage_status = _roll_up(statuses.values())

        if request.coverage.expected == "full" and coverage_status != "full":
            # Never publish a partial answer to a full-coverage slice; the
            # producer needs a truthful failure so it can retry or escalate.
            return ReportingSourceExecutorResult.failed(
                ReportingSourceErrorV1(
                    code="PARTIAL_RESULT",
                    retry="retryable",
                    safe_message=(
                        "the reporting source covered only part of the requested denominator; "
                        "a full-coverage slice cannot complete partially"
                    ),
                )
            )

        payload = _encode_rows(result.rows)
        object_ref, object_generation = await self._staging.stage(
            account_id=request.identity.account_id,
            source_execution_key=request.identity.source_execution_key,
            ordinal=0,
            payload=payload,
        )
        staged = SourceBatchObjectV1(
            ordinal=0,
            object_ref=object_ref,
            object_generation=object_generation,
            media_type="application/x-ndjson",
            compression="none",
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
            row_count=len(result.rows),
        )

        available = {
            constituent_id
            for constituent_id, status in statuses.items()
            if status in {"present", "explicit_zero"}
        }
        explicit_zero = bool(available) and not result.rows and available == set(statuses)
        manifest = self._seal_manifest(
            request,
            observed_at=observed_at,
            data_through=data_through,
            staged=staged,
            statuses=statuses,
            coverage_status=coverage_status,
            reasons=dict(result.unavailable_constituents),
            explicit_zero=explicit_zero,
            row_count=len(result.rows),
            control_totals=_control_totals(request, result.rows),
            warnings=warnings,
            provisional_until=result.provisional_until,
        )

        manifest_bytes = encode_source_batch_manifest_v1(manifest)
        reference = source_batch_manifest_reference_v1(
            f"{self._staged_commit_prefix}.{manifest.publication_id}", manifest_bytes
        )
        winner = await self._seals.put(
            account_id=request.identity.account_id,
            source_execution_key=request.identity.source_execution_key,
            sealed=SealedSlice(reference=reference, manifest_bytes=manifest_bytes),
        )
        # A concurrent worker may have sealed first; its bytes are the
        # publication, and returning ours instead would make the same key
        # resolve two ways.
        return ReportingSourceExecutorResult.completed(
            request=request, manifest=winner.reference, manifest_bytes=winner.manifest_bytes
        )

    def _derive_statuses(
        self,
        request: ReportingSourceSliceRequestV1,
        result: InlineFetchResult,
        covered: set[str],
        rows_by_constituent: Mapping[str, Sequence[Mapping[str, Any]]],
        data_through: datetime,
    ) -> dict[str, ReportingAvailabilityStatus]:
        """Turn "what came back" into per-constituent publication evidence.

        The freshness gate lives here.  An ``AUTHORITATIVE`` close whose
        watermark has not reached the period end cannot claim ``present``: the
        numbers are real but the window is not finished, which is ``delayed``,
        not complete.
        """
        short_of_the_window = request.publication_class == "AUTHORITATIVE" and _utc(
            data_through
        ) < _utc(request.period.end)
        statuses: dict[str, ReportingAvailabilityStatus] = {}
        for item in request.coverage.constituents:
            constituent_id = item.constituent_id
            if constituent_id in result.unavailable_constituents:
                statuses[constituent_id] = result.unavailable_status
            elif constituent_id not in covered:
                statuses[constituent_id] = "missing"
            elif short_of_the_window:
                statuses[constituent_id] = "delayed"
            elif rows_by_constituent.get(constituent_id):
                statuses[constituent_id] = "present"
            else:
                statuses[constituent_id] = "explicit_zero"
        return statuses

    def _seal_manifest(
        self,
        request: ReportingSourceSliceRequestV1,
        *,
        observed_at: datetime,
        data_through: datetime,
        staged: SourceBatchObjectV1,
        statuses: Mapping[str, ReportingAvailabilityStatus],
        coverage_status: str,
        reasons: Mapping[str, str],
        explicit_zero: bool,
        row_count: int,
        control_totals: list[SourceControlTotalV1],
        warnings: list[str],
        provisional_until: datetime | None,
    ) -> SourceBatchManifestV1:
        available = {"present", "explicit_zero"}

        def evidence(constituent_id: str) -> dict[str, Any]:
            status = statuses[constituent_id]
            if status in available:
                return {"data_through": data_through}
            return {"reason": reasons.get(constituent_id) or _DEFAULT_REASONS[status]}

        coverage = SourceBatchCoverageV1(
            denominator_fingerprint=request.coverage.denominator_fingerprint,
            status=coverage_status,  # type: ignore[arg-type]
            constituents=[
                ReportingConstituentCoverageV1(
                    constituent=item,
                    status=statuses[item.constituent_id],
                    **evidence(item.constituent_id),
                )
                for item in request.coverage.constituents
            ],
        )
        offering = self._capabilities.offering(request.offering_id)
        declared = {metric.name: metric for metric in offering.metrics}
        cells = [
            ReportingMetricAvailabilityV1(
                constituent_id=item.constituent_id,
                metric=metric,
                semantic_contract_id=declared[metric].semantic_contract_id,
                semantic_contract_version=declared[metric].semantic_contract_version,
                semantic_contract_sha256=declared[metric].semantic_contract_sha256,
                status=statuses[item.constituent_id],
                **evidence(item.constituent_id),
            )
            for item in request.coverage.constituents
            for metric in request.requested_metrics
        ]
        finality_at = (
            max(_utc(observed_at), _utc(request.period.end))
            if request.publication_class == "AUTHORITATIVE"
            else _utc(observed_at)
        )
        draft: dict[str, Any] = {
            "strictness": "basic",
            "identity": request.identity,
            "publication_namespace": request.publication_namespace,
            "publication_id": deterministic_source_publication_id_v1(
                source_execution_key=request.identity.source_execution_key,
                logical_slice_fingerprint=request.identity.logical_slice_fingerprint,
                publication_class=request.publication_class,
                revision_kind=request.revision_kind,
                supersedes_publication_id=request.supersedes_publication_id,
            ),
            "publication_class": request.publication_class,
            "content_fingerprint": f"sha256:{'0' * 64}",
            "adapter_build": request.adapter_build,
            "offering_id": request.offering_id,
            "contract": request.contract,
            "period": request.period,
            "revision_kind": request.revision_kind,
            "supersedes_publication_id": request.supersedes_publication_id,
            "finality_evidence": ReportingFinalityEvidenceV1(
                basis=(
                    "provisional_observation"
                    if request.publication_class == "PROVISIONAL_SNAPSHOT"
                    else "elapsed_settlement_window"
                ),
                observed_at=finality_at,
                provisional_until=(
                    provisional_until
                    if request.publication_class == "PROVISIONAL_SNAPSHOT"
                    else None
                ),
            ),
            "observed_at": observed_at,
            "data_through": data_through,
            "acquired_at": max(_utc(observed_at), finality_at),
            "currency": request.currency,
            "requested_dimensions": list(request.requested_dimensions),
            "objects": [staged],
            "object_set_sha256": source_batch_object_set_sha256_v1([staged]),
            "row_count": row_count,
            "byte_count": staged.byte_count,
            "control_totals": control_totals,
            "metric_availability": cells,
            "coverage": coverage,
            "explicit_zero": explicit_zero,
            "event_time_range": None if row_count == 0 else (request.period.start, data_through),
            "prior_checkpoint": request.prior_checkpoint,
            "warnings": warnings,
        }
        draft["content_fingerprint"] = publication_content_fingerprint_v1(
            SourceBatchManifestV1.model_construct(**draft)
        )
        return SourceBatchManifestV1(**draft)


_DEFAULT_REASONS: Mapping[str, str] = {
    "unsupported": "The reporting source does not serve this scope",
    "delayed": "The reporting source has not finalized this window yet",
    "partial": "The reporting source covered this scope only partially",
    "stale": "The reporting source returned data older than this window",
    "missing": "The reporting source returned no answer for this scope",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    moment = datetime.now(timezone.utc)
    return moment.replace(microsecond=(moment.microsecond // 1000) * 1000)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _clamp_watermark(
    request: ReportingSourceSliceRequestV1, declared: datetime | None, observed_at: datetime
) -> datetime:
    """Bound a declared watermark by the frozen slice and by the observation.

    Three ceilings, all of them real:

    * the **period end**, because data past it belongs to the next period;
    * the frozen **source read cutoff**, because the producer asked about a
      window ending there and a wider answer is a different question answered;
    * the **observation instant**, because nothing can have data through a time
      later than the moment it was looked at.

    The third is the one that catches a mid-period read: a snapshot taken at
    noon does not know about the evening, and clamping is what turns that into
    honest ``delayed`` evidence instead of a complete-looking publication.
    """
    ceiling = min(
        _utc(request.period.end),
        _utc(request.period.source_read_cutoff_at),
        _utc(observed_at),
    )
    bounded = ceiling if declared is None else min(_utc(declared), ceiling)
    return max(bounded, _utc(request.period.start))


def _roll_up(statuses: Collection[ReportingAvailabilityStatus]) -> str:
    available = sum(1 for status in statuses if status in {"present", "explicit_zero"})
    partial = sum(1 for status in statuses if status == "partial")
    if available == len(statuses) and statuses:
        return "full"
    if available or partial:
        return "partial"
    return "none"


def _encode_rows(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize rows as NDJSON with sorted keys.

    Sorted keys because the object digest is part of the manifest: two runs
    that produce the same rows in a different dict order must produce the same
    bytes, or replay identity fails for a reason nobody will enjoy debugging.
    """
    return b"".join(
        json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        + b"\n"
        for row in rows
    )


def _control_totals(
    request: ReportingSourceSliceRequestV1, rows: Sequence[Mapping[str, Any]]
) -> list[SourceControlTotalV1]:
    """Sum each requested metric across the rows, exactly.

    Money sums through :class:`~decimal.Decimal` and is emitted as a string.
    A float total would round differently in two languages and turn a
    consumer's equality check into a flake.
    """
    totals: list[SourceControlTotalV1] = []
    for metric in request.requested_metrics:
        values = [row[metric] for row in rows if row.get(metric) is not None]
        if len(values) != len(rows):
            # A metric absent from some rows has no honest total; the
            # per-cell availability evidence already carries the nuance.
            continue
        try:
            decimals = [_decimal(value) for value in values]
        except (InvalidOperation, TypeError, ValueError):
            continue
        if any(not item.is_finite() for item in decimals):
            # A NaN or Infinity in a source row has no honest total. Skipping
            # the metric leaves the per-cell availability evidence to carry the
            # nuance, rather than publishing a control total nobody can check
            # against.
            continue
        total = sum(decimals, Decimal(0))
        # The *metric* decides integer vs decimal, not this period's values.
        # Deciding per value would flip a money total to "integer" on the days
        # it happens to land on a whole number, and a consumer comparing
        # totals across periods would see the type change under it.
        scaled = any(_scale_of(item) < 0 for item in decimals)
        if scaled:
            totals.append(
                SourceControlTotalV1(
                    name=metric,
                    value=_plain(total),
                    value_type="decimal",
                    unit=request.currency if metric == "spend" else None,
                )
            )
        else:
            totals.append(
                SourceControlTotalV1(
                    name=metric,
                    value=str(int(total)),
                    value_type="integer",
                    unit=request.currency if metric == "spend" else None,
                )
            )
    return totals


def _scale_of(value: Decimal) -> int:
    """The decimal scale, or 0 for a value that has none."""
    exponent = value.as_tuple().exponent
    return exponent if isinstance(exponent, int) else 0


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("a boolean is not a reportable metric value")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # A float carries no meaningful scale -- Python renders 10.0 for what
        # the wire said was 10 -- so an integral float contributes none. Only a
        # genuinely fractional value does, and it goes through ``repr`` rather
        # than ``Decimal(float)`` so 1.25 stays 1.25 instead of becoming its
        # full binary expansion.
        return Decimal(int(value)) if value.is_integer() else Decimal(repr(value))
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"{type(value).__name__} is not a reportable metric value")


def _plain(value: Decimal) -> str:
    """Positional notation, with the source's declared scale preserved.

    Deliberately not ``normalize()``: "4.80" and "4.8" are the same number but
    different strings, and a money total's scale is information the source
    chose to publish. A consumer comparing control totals compares strings.
    """
    text = format(value, "f")
    return text if text != "-0" else "0"


def _default_constituent_of(
    row: Mapping[str, Any], request: ReportingSourceSliceRequestV1
) -> str | None:
    """Match a row to a constituent by the identifiers the row carries.

    Most-specific wins: a row naming a package matches the package-item
    constituent rather than the media-buy one that contains it, so a per-package
    cache does not collapse into a single buy-level cell.
    """
    package_id = _text(row.get("package_id"))
    media_buy_id = _text(row.get("media_buy_id"))
    product_id = _text(row.get("product_id"))
    constituent_id = _text(row.get("constituent_id"))
    for item in request.coverage.constituents:
        if constituent_id is not None and item.constituent_id == constituent_id:
            return item.constituent_id
    for item in request.coverage.constituents:
        if isinstance(item, PackageItemConstituentV1) and item.package_id == package_id:
            return item.constituent_id
    for item in request.coverage.constituents:
        if isinstance(item, MediaBuyConstituentV1) and item.media_buy_id == media_buy_id:
            return item.constituent_id
    for item in request.coverage.constituents:
        if isinstance(item, ProductConstituentV1) and item.product_id == product_id:
            return item.constituent_id
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _is_async_callable(fetch: InlineFetch) -> bool:
    if asyncio.iscoroutinefunction(fetch):
        return True
    call = getattr(type(fetch), "__call__", None)
    return call is not None and asyncio.iscoroutinefunction(call)


def _coerce(answer: InlineFetchReturn, *, currency: str) -> InlineFetchResult:
    """Normalize whatever the fetch returned into an :class:`InlineFetchResult`."""
    if isinstance(answer, InlineFetchResult):
        return answer
    rows = _delivery_rows(answer, currency=currency)
    if rows is not None:
        return rows
    if isinstance(answer, Sequence) and not isinstance(answer, (str, bytes)):
        return InlineFetchResult(rows=[dict(row) for row in answer])
    raise ReportingSourceError(
        "INVALID_REQUEST",
        f"the reporting source returned {type(answer).__name__}, which is not rows, "
        "an InlineFetchResult, a GetMediaBuyDeliveryResponse, or None",
    )


def _declared_currency(holder: Any) -> Any:
    """The currency a delivery object labels itself with, or ``None``.

    ``GetMediaBuyDeliveryResponse.currency`` is deprecated in AdCP 3.2 and
    Pydantic warns on every attribute read of it. Checking a legacy
    response-wide label against the frozen request is deliberate -- a
    contradiction there still has to fail the slice -- so read the stored
    value rather than emitting a DeprecationWarning per fetch.
    """
    stored = getattr(holder, "__dict__", None)
    if isinstance(stored, dict) and "currency" in stored:
        return stored["currency"]
    return getattr(holder, "currency", None)


def _delivery_rows(answer: Any, *, currency: str) -> InlineFetchResult | None:
    """Project an AdCP ``GetMediaBuyDeliveryResponse`` into normalized rows.

    Prefers the response's own ``reporting_rows`` when present -- those are
    already the seller's normalized grain.  Otherwise flattens
    ``media_buy_deliveries[].by_package[]`` into one row per package, which is
    the grain a reporting profile almost always wants.
    """
    deliveries = getattr(answer, "media_buy_deliveries", None)
    reporting_rows = getattr(answer, "reporting_rows", None)
    if deliveries is None and reporting_rows is None:
        return None
    # Flattening must not discard a response/media-buy/package denomination
    # before it can contradict the frozen request. These are source hints,
    # never a way to choose the obligation's currency.
    currency_holders = [answer]
    for delivery in deliveries or ():
        currency_holders.append(delivery)
        currency_holders.extend(getattr(delivery, "by_package", None) or ())
        for window in getattr(delivery, "windows", None) or ():
            currency_holders.extend(getattr(window, "by_package", None) or ())
    validate_row_currencies(
        currency,
        [
            {"currency": observed}
            for holder in currency_holders
            if (observed := _declared_currency(holder)) is not None
        ],
    )
    period = getattr(answer, "reporting_period", None)
    data_through = getattr(period, "end", None) if period is not None else None
    if reporting_rows:
        return InlineFetchResult(
            rows=[dict(row) for row in reporting_rows], data_through=data_through
        )
    rows: list[dict[str, Any]] = []
    for delivery in deliveries or []:
        media_buy_id = getattr(delivery, "media_buy_id", None)
        packages = getattr(delivery, "by_package", None) or []
        if not packages:
            rows.append(
                {"media_buy_id": media_buy_id, **_metrics(getattr(delivery, "totals", None))}
            )
            continue
        for package in packages:
            rows.append(
                {
                    "media_buy_id": media_buy_id,
                    "package_id": getattr(package, "package_id", None),
                    **_metrics(package),
                }
            )
    return InlineFetchResult(rows=rows, data_through=data_through)


_DELIVERY_METRICS = (
    "impressions",
    "spend",
    "clicks",
    "completed_views",
    "views",
    "conversions",
    "conversion_value",
)


def _metrics(holder: Any) -> dict[str, Any]:
    """Pull the reportable metrics off a totals-shaped object.

    Only metrics the holder actually carries: an omitted metric and a measured
    zero are different facts, and flattening the first into the second is how a
    buyer ends up reconciling against a zero that was never reported.
    """
    if holder is None:
        return {}
    values: dict[str, Any] = {}
    for metric in _DELIVERY_METRICS:
        value = getattr(holder, metric, None)
        if value is not None:
            values[metric] = value
    return values
