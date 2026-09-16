"""Executable conformance for reporting source executors.

An executor that type-checks is not necessarily a conforming executor.  These
validators are the difference: run them in your own test suite against your own
executor and they will tell you, before a buyer does, that you widened a date
range, published a partial result as complete, minted your own publication id,
or returned a different manifest the second time the same
``source_execution_key`` was replayed.

Three levels, each strictly stronger:

:func:`validate_reporting_source_execution`
    One execution.  Checks the slice against the capability declaration, the
    manifest against the slice, and every staged object's bytes against the
    digest the manifest claims for it.

:func:`run_reporting_source_replay_conformance`
    Two executions of the *same* ``source_execution_key``.  Both must validate
    and both must produce byte-identical manifests.  This is the check that
    catches an executor that embeds ``now()`` or a fresh UUID in its manifest.

:func:`validate_reporting_revision_sequence`
    A whole revision sequence.  Checks the rules no single manifest can:
    freshness never regresses, availability never silently degrades, a snapshot
    never follows an official publication, and a correction names the exact
    publication it supersedes.

Failures raise :class:`ReportingSourceConformanceError` with a stable code, so
a harness can assert *which* rule was broken rather than string-matching a
message.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar

from adcp.reporting.source import (
    AuthoritativeOfferingV1,
    ProvisionalSnapshotOfferingV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceErrorCode,
    ReportingSourceErrorV1,
    ReportingSourceExecutor,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
    ReportingSourceStagedObjectReader,
    SourceBatchManifestV1,
    deterministic_source_publication_id_v1,
    iso_duration_milliseconds_v1,
    parse_verified_source_batch_manifest_v1,
    source_batch_object_set_sha256_v1,
)

__all__ = [
    "ReportingSourceConformanceError",
    "ReportingSourceConformanceCode",
    "run_reporting_source_replay_conformance",
    "validate_reporting_revision_sequence",
    "validate_reporting_source_execution",
    "validate_reporting_source_failure",
]

ReportingSourceConformanceCode = Literal[
    "CAPABILITY_MISMATCH",
    "EXECUTION_FAILED",
    "IDENTITY_MISMATCH",
    "MANIFEST_MISMATCH",
    "OBJECT_MISMATCH",
    "REPLAY_MISMATCH",
    "REVISION_SEQUENCE_INVALID",
]

_AVAILABLE = frozenset({"present", "explicit_zero"})

_T = TypeVar("_T")


class ReportingSourceConformanceError(AssertionError):
    """An executor violated the source contract.

    Subclasses :class:`AssertionError` so an unhandled one reads like the test
    failure it almost always is, while still carrying a machine-checkable
    :attr:`code`.
    """

    def __init__(self, code: ReportingSourceConformanceCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: ReportingSourceConformanceCode, message: str) -> ReportingSourceConformanceError:
    return ReportingSourceConformanceError(code, message)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Deadline plumbing
# --------------------------------------------------------------------------


async def _with_deadline(
    operation: Callable[[], Awaitable[_T]],
    *,
    deadline_at: datetime,
    cancel: asyncio.Event,
    clock: Callable[[], datetime] | None = None,
) -> _T:
    """Run ``operation`` under the slice deadline, cancelling cooperatively first.

    On timeout the ``cancel`` event is set *before* the task is cancelled, so a
    conforming executor gets its documented chance to tear down upstream work
    and settle.  An executor that ignores the event is then hard-cancelled --
    the harness stays bounded even when the thing it is grading does not.
    """
    now = clock() if clock is not None else datetime.now(timezone.utc)
    remaining = (_utc(deadline_at) - _utc(now)).total_seconds()
    if cancel.is_set() or remaining <= 0:
        raise _fail(
            "EXECUTION_FAILED",
            "reporting source conformance execution was cancelled or exceeded its deadline",
        )
    task = asyncio.ensure_future(operation())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except asyncio.TimeoutError as error:
        cancel.set()
        task.cancel()
        # Observe the cancellation so a late failure cannot surface as an
        # "exception was never retrieved" warning long after the test passed.
        await asyncio.gather(task, return_exceptions=True)
        raise _fail(
            "EXECUTION_FAILED",
            "reporting source conformance execution exceeded its deadline",
        ) from error
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


# --------------------------------------------------------------------------
# Capability admission
# --------------------------------------------------------------------------


def _validate_request_against_capabilities(
    capabilities: ReportingSourceCapabilitiesV1, request: ReportingSourceSliceRequestV1
) -> ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1:
    """Is this frozen slice an exact atomic offering of this executor build?"""
    if capabilities.scope != "effective_account":
        raise _fail(
            "CAPABILITY_MISMATCH",
            "only an effective_account capability declaration is executable; a "
            "product_declaration describes what the adapter could do, not what these "
            "credentials may do",
        )
    if capabilities.adapter_build != request.adapter_build:
        raise _fail(
            "CAPABILITY_MISMATCH", "the slice names a different adapter build than this executor"
        )
    if capabilities.source_scope != request.identity.source_scope:
        raise _fail(
            "CAPABILITY_MISMATCH",
            "the slice source_scope does not match the bound capability scope",
        )
    try:
        offering = capabilities.offering(request.offering_id)
    except KeyError as error:
        raise _fail(
            "CAPABILITY_MISMATCH",
            f"offering {request.offering_id!r} is absent from the capability declaration",
        ) from error

    if (
        offering.publication_namespace != request.publication_namespace
        or offering.publication_class != request.publication_class
        or offering.contract != request.contract
        or offering.grain != request.period.grain
        or offering.windowing != request.period.windowing
    ):
        raise _fail(
            "CAPABILITY_MISMATCH",
            "the frozen slice is not an exact atomic offering of this executor build",
        )
    if (
        offering.source_timezone is not None
        and offering.source_timezone != request.period.source_timezone
    ):
        raise _fail(
            "CAPABILITY_MISMATCH", "the slice timezone is outside the bound account capability"
        )

    _validate_window_bounds(offering, request)

    for constituent in request.coverage.constituents:
        if constituent.product_id not in offering.product_ids:
            raise _fail(
                "CAPABILITY_MISMATCH",
                f"product {constituent.product_id!r} is outside the selected offering",
            )
        if constituent.constituent_kind not in offering.constituent_kinds:
            raise _fail(
                "CAPABILITY_MISMATCH",
                f"constituent kind {constituent.constituent_kind!r} is outside the "
                "selected offering",
            )

    if request.revision_kind == "correction" and (
        not isinstance(offering, AuthoritativeOfferingV1)
        or offering.correction_policy != "immutable_correction"
    ):
        raise _fail(
            "CAPABILITY_MISMATCH",
            "the selected offering does not support immutable corrections",
        )

    exact_metrics = {metric.name for metric in offering.metrics if metric.support == "exact"}
    for metric in request.requested_metrics:
        if metric not in exact_metrics:
            raise _fail(
                "CAPABILITY_MISMATCH", f"requested metric {metric!r} is not exact in the offering"
            )
    exact_dimensions = {
        dimension.name for dimension in offering.dimensions if dimension.support == "exact"
    }
    for dimension in request.requested_dimensions:
        if dimension not in exact_dimensions:
            raise _fail(
                "CAPABILITY_MISMATCH",
                f"requested dimension {dimension!r} is not exact in the offering",
            )
    return offering


def _validate_window_bounds(
    offering: ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1,
    request: ReportingSourceSliceRequestV1,
) -> None:
    """Reject a slice wider or narrower than the offering can honestly serve.

    Day-denominated windows compare in the *source calendar*, not in elapsed
    milliseconds: a DST-shortened local day is still one ``P1D`` window, and
    comparing it as 23 hours would reject a legitimate daily slice twice a year.
    """
    elapsed_ms = int(
        (_utc(request.period.end) - _utc(request.period.start)).total_seconds() * 1_000
    )
    calendar_ms = _source_calendar_elapsed_ms(request)
    minimum = iso_duration_milliseconds_v1(offering.windowing.minimum_window)
    maximum = iso_duration_milliseconds_v1(offering.windowing.maximum_window)
    minimum_basis = (
        calendar_ms if offering.windowing.minimum_window.lstrip("P")[:1].isdigit() else elapsed_ms
    )
    maximum_basis = (
        calendar_ms if offering.windowing.maximum_window.lstrip("P")[:1].isdigit() else elapsed_ms
    )
    provider_maximum_ms = (
        offering.provider_execution.maximum_window_days_per_request * 24 + 2
    ) * 3_600_000
    if minimum_basis < minimum or maximum_basis > maximum or elapsed_ms > provider_maximum_ms:
        raise _fail(
            "CAPABILITY_MISMATCH",
            "the requested slice is outside the offering's declared window bounds",
        )


def _source_calendar_elapsed_ms(request: ReportingSourceSliceRequestV1) -> int:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(request.period.source_timezone)
    start = request.period.start.astimezone(zone).replace(tzinfo=None)
    end = request.period.end.astimezone(zone).replace(tzinfo=None)
    return int((end - start).total_seconds() * 1_000)


# --------------------------------------------------------------------------
# Manifest admission
# --------------------------------------------------------------------------


def _validate_manifest_against_request(
    offering: ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1,
    request: ReportingSourceSliceRequestV1,
    manifest: SourceBatchManifestV1,
) -> None:
    if request.coverage.expected == "full" and manifest.coverage.status != "full":
        raise _fail(
            "MANIFEST_MISMATCH",
            "a full-coverage request must not complete with partial or missing coverage; "
            "the executor must return PARTIAL_RESULT and publish nothing",
        )
    if manifest.strictness != offering.manifest_strictness:
        raise _fail(
            "MANIFEST_MISMATCH",
            f"the offering declares {offering.manifest_strictness!r} manifests but the "
            f"executor published {manifest.strictness!r}",
        )
    declared_formats = {(item.media_type, item.compression) for item in offering.formats}
    for item in manifest.objects:
        if (item.media_type, item.compression) not in declared_formats:
            raise _fail(
                "CAPABILITY_MISMATCH",
                "every staged object format must be declared by the selected offering",
            )
    if manifest.evidence is not None:
        if manifest.evidence.pagination_kind != offering.provider_execution.pagination:
            raise _fail(
                "MANIFEST_MISMATCH",
                "manifest pagination evidence must match the selected offering",
            )
        used_jobs = bool(manifest.evidence.jobs)
        if offering.provider_execution.async_jobs == "required" and not used_jobs:
            raise _fail(
                "CAPABILITY_MISMATCH", "the offering requires async jobs but none were evidenced"
            )
        if offering.provider_execution.async_jobs == "unsupported" and used_jobs:
            raise _fail(
                "CAPABILITY_MISMATCH", "the offering does not support async jobs but jobs were run"
            )
        if manifest.evidence.pagination_kind == "cursor" and manifest.evidence.pages[
            0
        ].request_cursor != (request.prior_checkpoint.cursor if request.prior_checkpoint else None):
            raise _fail(
                "MANIFEST_MISMATCH",
                "cursor pagination must begin at the frozen prior checkpoint, or at the "
                "source origin when there is none",
            )
    if manifest.prior_checkpoint != request.prior_checkpoint:
        raise _fail(
            "MANIFEST_MISMATCH",
            "manifest prior-checkpoint evidence must exactly match the frozen request checkpoint",
        )

    expected_publication_id = deterministic_source_publication_id_v1(
        source_execution_key=request.identity.source_execution_key,
        logical_slice_fingerprint=request.identity.logical_slice_fingerprint,
        publication_class=request.publication_class,
        revision_kind=request.revision_kind,
        supersedes_publication_id=request.supersedes_publication_id,
    )
    echoed: list[tuple[str, Any, Any]] = [
        ("identity", manifest.identity, request.identity),
        ("publication_id", manifest.publication_id, expected_publication_id),
        ("publication_namespace", manifest.publication_namespace, request.publication_namespace),
        ("publication_class", manifest.publication_class, request.publication_class),
        ("adapter_build", manifest.adapter_build, request.adapter_build),
        ("offering_id", manifest.offering_id, request.offering_id),
        ("contract", manifest.contract, request.contract),
        ("period", manifest.period, request.period),
        ("revision_kind", manifest.revision_kind, request.revision_kind),
        (
            "supersedes_publication_id",
            manifest.supersedes_publication_id,
            request.supersedes_publication_id,
        ),
        ("currency", manifest.currency, request.currency),
        (
            "requested_dimensions",
            list(manifest.requested_dimensions),
            list(request.requested_dimensions),
        ),
        (
            "coverage.denominator_fingerprint",
            manifest.coverage.denominator_fingerprint,
            request.coverage.denominator_fingerprint,
        ),
    ]
    for field, actual, expected in echoed:
        if actual != expected:
            raise _fail(
                "MANIFEST_MISMATCH",
                f"manifest {field} does not bind the frozen slice request "
                f"({actual!r} != {expected!r})",
            )

    for total in manifest.control_totals:
        # Other monetary names need the ledger's trusted definition binding;
        # a three-letter unit alone does not establish monetary semantics.
        if total.unit is not None and total.name == "spend" and total.unit != request.currency:
            raise _fail(
                "MANIFEST_MISMATCH", "monetary control total unit contradicts frozen currency"
            )

    requested_metrics = set(request.requested_metrics)
    requested_constituents = {item.constituent_id for item in request.coverage.constituents}
    declared = {metric.name: metric for metric in offering.metrics}
    published_cells = {
        (cell.constituent_id, cell.metric): cell for cell in manifest.metric_availability
    }
    for cell in manifest.metric_availability:
        if (
            cell.metric not in requested_metrics
            or cell.constituent_id not in requested_constituents
        ):
            raise _fail(
                "MANIFEST_MISMATCH",
                "the manifest published a cell outside the requested constituent-metric matrix",
            )
    for constituent_id in requested_constituents:
        for metric in requested_metrics:
            published = published_cells.get((constituent_id, metric))
            if published is None:
                raise _fail(
                    "MANIFEST_MISMATCH",
                    f"no availability evidence for cell ({constituent_id!r}, {metric!r})",
                )
            contract = declared[metric]
            if (
                published.semantic_contract_id != contract.semantic_contract_id
                or published.semantic_contract_version != contract.semantic_contract_version
                or published.semantic_contract_sha256 != contract.semantic_contract_sha256
            ):
                raise _fail(
                    "MANIFEST_MISMATCH",
                    f"cell ({constituent_id!r}, {metric!r}) does not bind the offering's "
                    "declared metric semantics",
                )
    if source_batch_object_set_sha256_v1(manifest.objects) != manifest.object_set_sha256:
        raise _fail(
            "MANIFEST_MISMATCH", "the manifest object-set checksum does not bind its objects"
        )


# --------------------------------------------------------------------------
# Public validators
# --------------------------------------------------------------------------


async def validate_reporting_source_execution(
    *,
    capabilities: ReportingSourceCapabilitiesV1,
    request: ReportingSourceSliceRequestV1,
    result: ReportingSourceExecutorResult,
    object_reader: ReportingSourceStagedObjectReader,
    cancel: asyncio.Event | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SourceBatchManifestV1:
    """Validate one execution end to end and return its verified manifest.

    Reads every staged object the manifest names and checks its bytes against
    the declared digest and size.  A manifest whose objects cannot be read, or
    read differently than claimed, is not evidence of anything.
    ``clock`` permits deterministic deadline checks for retained test slices.
    """
    offering = _validate_request_against_capabilities(capabilities, request)
    if not result.ok:
        error = result.error
        assert error is not None  # guaranteed by ReportingSourceExecutorResult
        raise _fail("EXECUTION_FAILED", f"{error.code}: {error.safe_message}")

    response = result.response
    assert response is not None
    manifest_bytes = result.manifest_bytes
    assert manifest_bytes is not None

    if response.identity != request.identity:
        raise _fail(
            "IDENTITY_MISMATCH", "the response must echo the complete frozen request identity"
        )

    manifest = parse_verified_source_batch_manifest_v1(response.manifest, manifest_bytes)
    _validate_manifest_against_request(offering, request, manifest)

    cancel = cancel or asyncio.Event()
    for item in manifest.objects:
        try:
            payload = await _with_deadline(
                lambda item=item: object_reader.read(  # type: ignore[misc]
                    object_ref=item.object_ref,
                    object_generation=item.object_generation,
                    account_id=request.identity.account_id,
                    source_scope=request.identity.source_scope,
                    cancel=cancel,
                ),
                deadline_at=request.deadline_at,
                cancel=cancel,
                clock=clock,
            )
        except ReportingSourceConformanceError:
            raise
        except Exception as error:
            raise _fail(
                "OBJECT_MISMATCH",
                f"the generation-pinned object {item.ordinal} is not readable",
            ) from error
        if len(payload) != item.byte_count or hashlib.sha256(payload).hexdigest() != item.sha256:
            raise _fail(
                "OBJECT_MISMATCH",
                f"the generation-pinned object {item.ordinal} does not match its evidence",
            )
    return manifest


async def run_reporting_source_replay_conformance(
    *,
    executor: ReportingSourceExecutor,
    request: ReportingSourceSliceRequestV1,
    object_reader: ReportingSourceStagedObjectReader,
    cancel: asyncio.Event | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SourceBatchManifestV1:
    """Execute the same slice twice and require an identical immutable publication.

    Reusing a ``source_execution_key`` must return byte-identical manifest bytes
    and the same staged object set.  This is the check that catches the two most
    common non-conformances: a ``now()`` timestamp baked into the manifest, and a
    fresh UUID minted per attempt. ``clock`` supplies the deadline-check instant
    for both executions and their staged-object reads.
    """
    capabilities = executor.capabilities
    cancel = cancel or asyncio.Event()

    async def once() -> tuple[ReportingSourceExecutorResult, SourceBatchManifestV1]:
        result = await _with_deadline(
            lambda: executor.execute(request, cancel=cancel),
            deadline_at=request.deadline_at,
            cancel=cancel,
            clock=clock,
        )
        manifest = await validate_reporting_source_execution(
            capabilities=capabilities,
            request=request,
            result=result,
            object_reader=object_reader,
            cancel=cancel,
            clock=clock,
        )
        return result, manifest

    first_result, first_manifest = await once()
    second_result, second_manifest = await once()
    first_reference = first_result.response.manifest if first_result.response else None
    second_reference = second_result.response.manifest if second_result.response else None
    if (
        first_reference != second_reference
        or first_result.manifest_bytes != second_result.manifest_bytes
        or first_manifest != second_manifest
    ):
        raise _fail(
            "REPLAY_MISMATCH",
            "repeating a source_execution_key must return the identical immutable publication",
        )
    return first_manifest


def validate_reporting_source_failure(
    result: ReportingSourceExecutorResult, expected_code: ReportingSourceErrorCode
) -> ReportingSourceErrorV1:
    """Assert a slice failed with exactly ``expected_code`` and return the error.

    A failed upstream page or job must not publish ``complete: true``; this is
    how you assert that in a test.
    """
    if result.ok:
        raise _fail(
            "EXECUTION_FAILED",
            "a failed or partial source fetch must not return a completed manifest",
        )
    error = result.error
    assert error is not None
    if error.code != expected_code:
        raise _fail("EXECUTION_FAILED", f"expected {expected_code}, received {error.code}")
    return error


# --------------------------------------------------------------------------
# Revision sequences
# --------------------------------------------------------------------------


def _semantic_scope(manifest: SourceBatchManifestV1) -> dict[str, Any]:
    return {
        "offering_id": manifest.offering_id,
        "publication_namespace": manifest.publication_namespace,
        "denominator_fingerprint": manifest.coverage.denominator_fingerprint,
        "contract": manifest.contract.canonical_payload(),
        "currency": manifest.currency,
        "requested_dimensions": sorted(manifest.requested_dimensions),
        "grain": manifest.period.grain,
        "windowing": manifest.period.windowing.canonical_payload(),
        "metrics": sorted(
            (
                cell.constituent_id,
                cell.metric,
                cell.semantic_contract_id,
                cell.semantic_contract_version,
                cell.semantic_contract_sha256,
            )
            for cell in manifest.metric_availability
        ),
    }


def _sequence_scope(manifest: SourceBatchManifestV1) -> dict[str, Any]:
    return {
        "account_id": manifest.identity.account_id,
        "delivery_config_id": manifest.identity.delivery_config_id,
        "report_definition_id": manifest.identity.report_definition_id,
        "source_scope": manifest.identity.source_scope,
        "period_key": manifest.period.period_key,
        "source_local_date": manifest.period.source_local_date,
        "start": _utc(manifest.period.start).isoformat(),
        "end": _utc(manifest.period.end).isoformat(),
        "source_timezone": manifest.period.source_timezone,
    }


def validate_reporting_revision_sequence(
    manifests: Sequence[SourceBatchManifestV1],
    *,
    allow_cross_finality: bool = False,
) -> None:
    """Validate an ordered sequence of publications for one logical window.

    The rules a single manifest cannot enforce:

    * every publication describes the same account, config, source scope, and
      logical window;
    * ``observed_at``, ``acquired_at``, ``source_read_cutoff_at``, and
      ``data_through`` never regress;
    * a constituent or metric cell that was once ``present`` / ``explicit_zero``
      never silently degrades, and its watermark never moves backwards --
      a later fetch may report *new* unavailability, but it may not replace a
      fresh committed cell with a stale one;
    * a snapshot never follows the official publication;
    * exactly one initial ``authoritative`` publication exists, and every later
      ``correction`` names its immediate predecessor.

    Snapshot and authoritative offerings usually have different semantic scope
    (different metrics, grain, or windowing), so mixing them in one sequence is
    refused unless ``allow_cross_finality`` says the caller has already
    established that the two scopes address the same buyer-visible window.
    """
    if not manifests:
        return
    first = manifests[0]
    baseline = _sequence_scope(first)
    for manifest in manifests:
        if _sequence_scope(manifest) != baseline:
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                "every revision in a sequence must describe the same account, source scope, "
                "and logical window",
            )

    has_snapshot = any(item.revision_kind == "snapshot" for item in manifests)
    has_official = any(item.revision_kind != "snapshot" for item in manifests)
    if has_snapshot and has_official and not allow_cross_finality:
        raise _fail(
            "REVISION_SEQUENCE_INVALID",
            "a snapshot-to-official transition needs an explicit cross-finality decision; "
            "pass allow_cross_finality=True once the two semantic scopes are known to "
            "address the same buyer-visible window",
        )

    prior_snapshot: SourceBatchManifestV1 | None = None
    official: SourceBatchManifestV1 | None = None
    for manifest in manifests:
        if manifest.revision_kind == "snapshot":
            if official is not None:
                raise _fail(
                    "REVISION_SEQUENCE_INVALID",
                    "a snapshot cannot replace or follow the official publication",
                )
            if prior_snapshot is not None:
                if _semantic_scope(prior_snapshot) != _semantic_scope(manifest):
                    raise _fail(
                        "REVISION_SEQUENCE_INVALID",
                        "a snapshot must retain the exact semantic scope of its predecessor",
                    )
                _assert_freshness_does_not_regress(prior_snapshot, manifest, "a snapshot")
                _assert_cells_do_not_regress(prior_snapshot, manifest, "a snapshot")
            prior_snapshot = manifest
            continue

        if manifest.revision_kind == "authoritative":
            if official is not None:
                raise _fail(
                    "REVISION_SEQUENCE_INVALID",
                    "only one initial authoritative publication is allowed; a later "
                    "restatement is a correction",
                )
            if prior_snapshot is not None:
                _assert_freshness_does_not_regress(
                    prior_snapshot, manifest, "an authoritative publication"
                )
                _assert_cells_do_not_regress(
                    prior_snapshot, manifest, "an authoritative publication", shared_only=True
                )
            official = manifest
            continue

        if official is None:
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                "a correction must follow an authoritative publication",
            )
        if manifest.supersedes_publication_id != official.publication_id:
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                "a correction must name the exact publication it supersedes",
            )
        if _semantic_scope(official) != _semantic_scope(manifest):
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                "a correction must retain the semantic scope of its predecessor",
            )
        _assert_freshness_does_not_regress(official, manifest, "a correction")
        _assert_cells_do_not_regress(official, manifest, "a correction")
        official = manifest


def _assert_freshness_does_not_regress(
    predecessor: SourceBatchManifestV1, successor: SourceBatchManifestV1, label: str
) -> None:
    for field, before, after in (
        ("observed_at", predecessor.observed_at, successor.observed_at),
        ("acquired_at", predecessor.acquired_at, successor.acquired_at),
        (
            "source_read_cutoff_at",
            predecessor.period.source_read_cutoff_at,
            successor.period.source_read_cutoff_at,
        ),
        ("data_through", predecessor.data_through, successor.data_through),
    ):
        if _utc(after) < _utc(before):
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                f"{label} must not regress {field} from its predecessor",
            )


def _assert_cells_do_not_regress(
    predecessor: SourceBatchManifestV1,
    successor: SourceBatchManifestV1,
    label: str,
    *,
    shared_only: bool = False,
) -> None:
    """Availability is independently monotonic from the batch watermark.

    Retaining an old batch watermark cannot disguise a cell that moved from
    ``present`` to unavailable.  A later fetch may legitimately report new
    delayed/partial/stale/missing coverage -- but it may not *replace* a cell
    the producer already committed as fresh with a degraded one, because a
    consumer that already read the fresh value would silently lose it.

    ``shared_only`` relaxes the comparison to cells that exist in both
    publications, which is what a snapshot-to-official transition needs: the
    two offerings may legitimately publish different metric sets.
    """
    successor_constituents = {item.constituent_id: item for item in successor.coverage.constituents}
    shared: set[str] = set()
    for item in predecessor.coverage.constituents:
        later = successor_constituents.get(item.constituent_id)
        if shared_only and (later is None or later.constituent != item.constituent):
            continue
        shared.add(item.constituent_id)
        if item.status in _AVAILABLE and (later is None or later.status not in _AVAILABLE):
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                f"{label} must not remove or degrade constituent availability from its "
                "predecessor",
            )
        if item.data_through is not None and (
            later is None
            or later.data_through is None
            or _utc(later.data_through) < _utc(item.data_through)
        ):
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                f"{label} must not regress constituent data-through evidence",
            )

    successor_cells = {
        (cell.constituent_id, cell.metric): cell for cell in successor.metric_availability
    }
    for cell in predecessor.metric_availability:
        later_cell = successor_cells.get((cell.constituent_id, cell.metric))
        if shared_only and (
            cell.constituent_id not in shared
            or later_cell is None
            or later_cell.semantic_contract_sha256 != cell.semantic_contract_sha256
        ):
            continue
        if cell.status in _AVAILABLE and (
            later_cell is None or later_cell.status not in _AVAILABLE
        ):
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                f"{label} must not remove or degrade metric availability from its predecessor",
            )
        if cell.data_through is not None and (
            later_cell is None
            or later_cell.data_through is None
            or _utc(later_cell.data_through) < _utc(cell.data_through)
        ):
            raise _fail(
                "REVISION_SEQUENCE_INVALID",
                f"{label} must not regress metric data-through evidence",
            )
