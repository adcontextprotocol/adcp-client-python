"""The reporting source contract, asserted as behavior.

Every test here names a rule a real adapter has broken at least once in a real
integration: publishing a partial fetch as complete, minting its own
publication id, widening a date range, confusing "nothing happened" with "the
source did not answer", or returning a different manifest on replay.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from adcp.reporting.conformance import (
    ReportingSourceConformanceError,
    run_reporting_source_replay_conformance,
    validate_reporting_revision_sequence,
    validate_reporting_source_execution,
    validate_reporting_source_failure,
)
from adcp.reporting.fixtures import (
    redacted_authoritative_request,
    redacted_capabilities,
    redacted_completed_result,
    redacted_snapshot_request,
)
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceError,
    ReportingSourceErrorV1,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
    SourceBatchManifestError,
    SourceBatchManifestV1,
    coverage_denominator_fingerprint_v1,
    deterministic_source_publication_id_v1,
    encode_source_batch_manifest_v1,
    parse_verified_source_batch_manifest_v1,
    publication_content_fingerprint_v1,
    source_batch_manifest_reference_v1,
)


class _StaticExecutor:
    """Returns a prepared result, optionally a different one the second time."""

    def __init__(
        self,
        capabilities: ReportingSourceCapabilitiesV1,
        results: list[ReportingSourceExecutorResult],
    ) -> None:
        self._capabilities = capabilities
        self._results = results
        self.calls = 0
        self.cancel_seen = False

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self._capabilities

    async def execute(self, request, *, cancel, heartbeat=None):
        self.calls += 1
        self.cancel_seen = cancel.is_set()
        return self._results[min(self.calls - 1, len(self._results) - 1)]


def _rebind(manifest: SourceBatchManifestV1, **updates) -> SourceBatchManifestV1:
    """Re-seal a manifest after an edit, recomputing its content fingerprint.

    Goes through full ``model_validate`` rather than ``model_copy``: Pydantic's
    copy deliberately skips validators, and every test below is asserting that
    a validator fires.
    """
    draft = SourceBatchManifestV1.model_construct(**{**dict(manifest), **updates})
    payload = draft.model_dump(mode="json", exclude_none=True)
    payload["content_fingerprint"] = publication_content_fingerprint_v1(draft)
    return SourceBatchManifestV1.model_validate(payload)


# -- capability admission ---------------------------------------------------


async def test_conforming_snapshot_execution_validates() -> None:
    request = redacted_snapshot_request()
    result, manifest, reader = redacted_completed_result(request)
    validated = await validate_reporting_source_execution(
        capabilities=redacted_capabilities(),
        request=request,
        result=result,
        object_reader=reader,
    )
    assert validated == manifest
    assert validated.coverage.status == "full"


async def test_product_declaration_capabilities_are_not_executable() -> None:
    # An integration-wide brochure must not masquerade as account eligibility.
    capabilities = redacted_capabilities()
    payload = capabilities.model_dump(mode="json")
    payload["scope"] = "product_declaration"
    payload.pop("capabilities_sha256")
    from adcp.reporting.source import reporting_source_capabilities_sha256_v1

    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
    declaration = ReportingSourceCapabilitiesV1.model_validate(payload)

    request = redacted_snapshot_request()
    result, _, reader = redacted_completed_result(request)
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=declaration, request=request, result=result, object_reader=reader
        )
    assert error.value.code == "CAPABILITY_MISMATCH"


async def test_unknown_offering_is_refused() -> None:
    request = redacted_snapshot_request().model_copy(update={"offering_id": "NOT_DECLARED"})
    result, _, reader = redacted_completed_result(redacted_snapshot_request())
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "CAPABILITY_MISMATCH"


async def test_metric_outside_the_offering_is_refused() -> None:
    base = redacted_snapshot_request()
    request = base.model_copy(update={"requested_metrics": ["impressions", "reach"]})
    result, _, reader = redacted_completed_result(base)
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "CAPABILITY_MISMATCH"


async def test_slice_wider_than_the_offering_window_is_refused() -> None:
    base = redacted_snapshot_request()
    wide = base.model_copy(
        update={
            "period": base.period.model_copy(update={"end": base.period.start + timedelta(days=9)})
        }
    )
    result, _, reader = redacted_completed_result(base)
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=wide,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "CAPABILITY_MISMATCH"


# -- manifest admission -----------------------------------------------------


async def test_manifest_must_echo_the_frozen_slice() -> None:
    request = redacted_snapshot_request()
    _, manifest, reader = redacted_completed_result(request)
    tampered = _rebind(manifest, currency="EUR")
    manifest_bytes = encode_source_batch_manifest_v1(tampered)
    result = ReportingSourceExecutorResult.completed(
        request=request,
        manifest=source_batch_manifest_reference_v1("manifest-redacted-v1", manifest_bytes),
        manifest_bytes=manifest_bytes,
    )
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "MANIFEST_MISMATCH"


async def test_executor_may_not_mint_its_own_publication_id() -> None:
    request = redacted_snapshot_request()
    _, manifest, reader = redacted_completed_result(request)
    tampered = _rebind(manifest, publication_id="src-chosen-by-the-adapter")
    manifest_bytes = encode_source_batch_manifest_v1(tampered)
    result = ReportingSourceExecutorResult.completed(
        request=request,
        manifest=source_batch_manifest_reference_v1("manifest-redacted-v1", manifest_bytes),
        manifest_bytes=manifest_bytes,
    )
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "MANIFEST_MISMATCH"


async def test_full_coverage_request_cannot_complete_partially() -> None:
    request = redacted_snapshot_request()
    result, _, reader = redacted_completed_result(request, constituent_status="delayed")
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "MANIFEST_MISMATCH"


async def test_partial_coverage_request_may_complete_with_no_coverage() -> None:
    base = redacted_snapshot_request()
    request = base.model_copy(
        update={"coverage": base.coverage.model_copy(update={"expected": "partial"})}
    )
    result, manifest, reader = redacted_completed_result(
        request, page_bodies=[""], row_counts=[0], constituent_status="delayed"
    )
    validated = await validate_reporting_source_execution(
        capabilities=redacted_capabilities(),
        request=request,
        result=result,
        object_reader=reader,
    )
    assert validated.coverage.status == "none"
    assert validated.explicit_zero is False


async def test_staged_object_bytes_must_match_their_declared_digest() -> None:
    request = redacted_snapshot_request()
    result, manifest, _ = redacted_completed_result(request)

    class WrongBytesReader:
        async def read(self, **_kwargs: object) -> bytes:
            return b"different bytes entirely"

    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=WrongBytesReader(),
        )
    assert error.value.code == "OBJECT_MISMATCH"


async def test_staged_object_reader_enforces_the_account_scope_fence() -> None:
    request = redacted_snapshot_request()
    result, _, reader = redacted_completed_result(request)
    other_account = request.model_copy(
        update={"identity": request.identity.model_copy(update={"account_id": "someone-else"})}
    )
    # Re-point only the *read* scope: the manifest still binds the original
    # identity, so this is the "opaque reference leaked to another tenant" case.
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=other_account,
            result=result,
            object_reader=reader,
        )
    assert error.value.code in {"IDENTITY_MISMATCH", "MANIFEST_MISMATCH", "OBJECT_MISMATCH"}


# -- zero rows --------------------------------------------------------------


async def test_observed_zero_and_unavailable_zero_are_different_publications() -> None:
    request = redacted_snapshot_request()
    _, observed_zero, _ = redacted_completed_result(request, page_bodies=[""], row_counts=[0])
    assert observed_zero.explicit_zero is True
    assert observed_zero.coverage.status == "full"
    assert observed_zero.event_time_range is None

    partial = request.model_copy(
        update={"coverage": request.coverage.model_copy(update={"expected": "partial"})}
    )
    _, unavailable, _ = redacted_completed_result(
        partial, page_bodies=[""], row_counts=[0], constituent_status="missing"
    )
    assert unavailable.explicit_zero is False
    assert unavailable.coverage.status == "none"


async def test_a_zero_row_available_batch_must_be_marked_explicitly_zero() -> None:
    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request, page_bodies=[""], row_counts=[0])
    with pytest.raises(ValidationError, match="must be explicitly zero"):
        _rebind(manifest, explicit_zero=False)


async def test_explicit_zero_cannot_carry_nonzero_totals() -> None:
    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request, page_bodies=[""], row_counts=[0])
    from adcp.reporting.source import SourceControlTotalV1

    with pytest.raises(ValidationError, match="explicit-zero control total must be zero"):
        _rebind(
            manifest,
            control_totals=[SourceControlTotalV1(name="impressions", value="10")],
        )


# -- time evidence ----------------------------------------------------------


async def test_data_through_cannot_follow_the_observation() -> None:
    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request)
    with pytest.raises(ValidationError, match="data_through must not follow observed_at"):
        _rebind(manifest, observed_at=manifest.data_through - timedelta(minutes=1))


async def test_authoritative_finality_cannot_predate_the_period_end() -> None:
    request = redacted_authoritative_request()
    _, manifest, _ = redacted_completed_result(request)
    early = manifest.finality_evidence.model_copy(
        update={"observed_at": request.period.end - timedelta(hours=1)}
    )
    with pytest.raises(ValidationError):
        _rebind(manifest, finality_evidence=early)


async def test_full_authoritative_coverage_must_prove_the_whole_window() -> None:
    request = redacted_authoritative_request()
    _, manifest, _ = redacted_completed_result(request)
    assert manifest.data_through == request.period.end
    short = manifest.data_through - timedelta(hours=1)
    with pytest.raises(ValidationError):
        _rebind(manifest, data_through=short)


# -- cell matrix ------------------------------------------------------------


async def test_every_constituent_needs_a_record_for_every_published_metric() -> None:
    # A constituent added to coverage without its cells is the shape this rule
    # exists for: the roll-up would claim coverage nothing underneath supports.
    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request)
    orphan = manifest.coverage.constituents[0]
    extended = [
        *manifest.coverage.constituents,
        orphan.model_copy(
            update={
                "constituent": orphan.constituent.model_copy(
                    update={"constituent_id": "campaign-redacted-2"}
                )
            }
        ),
    ]
    with pytest.raises(ValidationError, match="every published metric"):
        _rebind(
            manifest,
            coverage=manifest.coverage.model_copy(
                update={
                    "constituents": extended,
                    "denominator_fingerprint": coverage_denominator_fingerprint_v1(
                        [item.constituent for item in extended]
                    ),
                }
            ),
        )


async def test_metric_status_cannot_contradict_its_constituent() -> None:
    base = redacted_snapshot_request()
    request = base.model_copy(
        update={"coverage": base.coverage.model_copy(update={"expected": "partial"})}
    )
    _, manifest, _ = redacted_completed_result(request, constituent_status="missing")
    # constituent is `missing`; a `present` cell underneath it is incoherent.
    contradictory = [
        manifest.metric_availability[0].model_copy(
            update={"status": "present", "data_through": manifest.data_through, "reason": None}
        ),
        *manifest.metric_availability[1:],
    ]
    with pytest.raises(ValidationError, match="contradicts constituent status"):
        _rebind(manifest, metric_availability=contradictory)


# -- errors -----------------------------------------------------------------


def test_error_codes_constrain_their_retry_classification() -> None:
    with pytest.raises(ValidationError, match="cannot be classified"):
        ReportingSourceErrorV1(
            code="AUTHENTICATION_FAILED", retry="retryable", safe_message="bad credential"
        )
    with pytest.raises(ValidationError, match="cannot be classified"):
        ReportingSourceErrorV1(code="RATE_LIMITED", retry="terminal", safe_message="slow down")
    assert (
        ReportingSourceErrorV1(
            code="RATE_LIMITED", retry="retryable", safe_message="slow down"
        ).retry
        == "retryable"
    )


def test_retry_after_is_reserved_for_backoff_evidence() -> None:
    with pytest.raises(ValidationError, match="reserved for upstream backoff"):
        ReportingSourceErrorV1(
            code="INVALID_REQUEST",
            retry="terminal",
            safe_message="malformed",
            retry_after_seconds=30,
        )


def test_raised_source_errors_default_to_the_safe_classification() -> None:
    assert ReportingSourceError("PROVIDER_PERMANENT", "gone").error.retry == "terminal"
    assert ReportingSourceError("PROVIDER_TRANSIENT", "flaky").error.retry == "retryable"
    assert ReportingSourceError("CANCELLED", "stopped").error.retry == "cancelled"
    # PARTIAL_RESULT permits both; the safe default is to stop, not to spin.
    assert ReportingSourceError("PARTIAL_RESULT", "one page missing").error.retry == "terminal"


def test_validate_failure_requires_the_expected_code() -> None:
    failure = ReportingSourceExecutorResult.failed(
        ReportingSourceErrorV1(
            code="PARTIAL_RESULT", retry="terminal", safe_message="a source page was lost"
        )
    )
    assert validate_reporting_source_failure(failure, "PARTIAL_RESULT").code == "PARTIAL_RESULT"
    with pytest.raises(ReportingSourceConformanceError):
        validate_reporting_source_failure(failure, "RATE_LIMITED")


async def test_a_completed_result_cannot_be_asserted_as_a_failure() -> None:
    request = redacted_snapshot_request()
    result, _, _ = redacted_completed_result(request)
    with pytest.raises(ReportingSourceConformanceError, match="must not return a completed"):
        validate_reporting_source_failure(result, "PARTIAL_RESULT")


def test_a_result_is_exactly_one_of_completed_or_failed() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ReportingSourceExecutorResult()


# -- replay -----------------------------------------------------------------


async def test_replaying_an_execution_key_must_return_identical_bytes() -> None:
    request = redacted_snapshot_request()
    result, manifest, reader = redacted_completed_result(request)
    executor = _StaticExecutor(redacted_capabilities(), [result])
    replayed = await run_reporting_source_replay_conformance(
        executor=executor, request=request, object_reader=reader
    )
    assert executor.calls == 2
    assert replayed == manifest


async def test_replay_that_changes_the_manifest_is_nonconforming() -> None:
    request = redacted_snapshot_request()
    first, _, reader_a = redacted_completed_result(request)
    # Same slice, different observation time: a manifest that embeds now().
    second, _, _ = redacted_completed_result(
        request, observed_at=request.period.source_read_cutoff_at + timedelta(minutes=5)
    )
    executor = _StaticExecutor(redacted_capabilities(), [first, second])
    with pytest.raises(ReportingSourceConformanceError) as error:
        await run_reporting_source_replay_conformance(
            executor=executor, request=request, object_reader=reader_a
        )
    assert error.value.code == "REPLAY_MISMATCH"


async def test_an_executor_past_its_deadline_is_cancelled_and_reported() -> None:
    base = redacted_snapshot_request()
    request = base.model_copy(
        update={"deadline_at": datetime.now(timezone.utc) + timedelta(milliseconds=80)}
    )
    _, _, reader = redacted_completed_result(base)

    class SleepyExecutor:
        def __init__(self) -> None:
            self.cancelled = False

        @property
        def capabilities(self) -> ReportingSourceCapabilitiesV1:
            return redacted_capabilities()

        async def execute(self, request, *, cancel, heartbeat=None):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    executor = SleepyExecutor()
    with pytest.raises(ReportingSourceConformanceError) as error:
        await run_reporting_source_replay_conformance(
            executor=executor, request=request, object_reader=reader
        )
    assert error.value.code == "EXECUTION_FAILED"
    assert executor.cancelled is True


# -- manifest bytes ---------------------------------------------------------


async def test_manifest_bytes_round_trip_through_verification() -> None:
    request = redacted_snapshot_request()
    result, manifest, _ = redacted_completed_result(request)
    assert result.response is not None and result.manifest_bytes is not None
    parsed = parse_verified_source_batch_manifest_v1(
        result.response.manifest, result.manifest_bytes
    )
    assert parsed == manifest


async def test_noncanonical_manifest_bytes_are_refused() -> None:
    request = redacted_snapshot_request()
    result, manifest, _ = redacted_completed_result(request)
    # Same logical manifest, pretty-printed: parses fine, is not canonical.
    pretty = manifest.model_dump_json(indent=2, exclude_none=True).encode("utf-8")
    reference = source_batch_manifest_reference_v1("manifest-redacted-v1", pretty)
    with pytest.raises(SourceBatchManifestError) as error:
        parse_verified_source_batch_manifest_v1(reference, pretty)
    assert error.value.code == "SOURCE_MANIFEST_CANONICAL_ENCODING_MISMATCH"


async def test_manifest_bytes_that_do_not_match_the_reference_are_refused() -> None:
    request = redacted_snapshot_request()
    result, _, _ = redacted_completed_result(request)
    assert result.response is not None and result.manifest_bytes is not None
    wrong = result.response.manifest.model_copy(update={"manifest_sha256": "0" * 64})
    with pytest.raises(SourceBatchManifestError) as error:
        parse_verified_source_batch_manifest_v1(wrong, result.manifest_bytes)
    assert error.value.code == "SOURCE_MANIFEST_CHECKSUM_MISMATCH"


# -- fingerprints -----------------------------------------------------------


def test_denominator_fingerprint_ignores_request_order() -> None:
    first = MediaBuyConstituentV1(constituent_id="a", product_id="p", media_buy_id="mb-1")
    second = MediaBuyConstituentV1(constituent_id="b", product_id="p", media_buy_id="mb-2")
    assert coverage_denominator_fingerprint_v1(
        [first, second]
    ) == coverage_denominator_fingerprint_v1([second, first])


def test_denominator_fingerprint_changes_when_a_constituent_is_rebound() -> None:
    original = MediaBuyConstituentV1(constituent_id="a", product_id="p", media_buy_id="mb-1")
    rebound = MediaBuyConstituentV1(constituent_id="a", product_id="p", media_buy_id="mb-2")
    assert coverage_denominator_fingerprint_v1([original]) != coverage_denominator_fingerprint_v1(
        [rebound]
    )


def test_publication_id_is_derived_not_chosen() -> None:
    identity = {
        "source_execution_key": "exec-00000001",
        "logical_slice_fingerprint": f"sha256:{'a' * 64}",
        "publication_class": "AUTHORITATIVE",
        "revision_kind": "authoritative",
    }
    assert deterministic_source_publication_id_v1(
        **identity
    ) == deterministic_source_publication_id_v1(**identity)
    assert deterministic_source_publication_id_v1(
        **{**identity, "revision_kind": "correction", "supersedes_publication_id": "src-prior"}
    ) != deterministic_source_publication_id_v1(**identity)


def test_content_fingerprint_ignores_acquisition_timing() -> None:
    # Two fetches of an unchanged quiet campaign must dedupe on payload while
    # still committing advanced freshness evidence.
    request = redacted_snapshot_request()
    _, early, _ = redacted_completed_result(request)
    _, late, _ = redacted_completed_result(
        request, observed_at=request.period.source_read_cutoff_at + timedelta(hours=1)
    )
    assert early.content_fingerprint == late.content_fingerprint
    assert early.observed_at != late.observed_at


# -- revision sequences -----------------------------------------------------


def _snapshot_at(minutes: int) -> SourceBatchManifestV1:
    """A snapshot whose read cutoff, observation, and watermark all advance together."""
    observed = redacted_snapshot_request().period.source_read_cutoff_at + timedelta(minutes=minutes)
    request = redacted_snapshot_request(
        source_execution_key=f"snapshot-exec-{minutes:04d}", source_read_cutoff_at=observed
    )
    _, manifest, _ = redacted_completed_result(request, observed_at=observed, data_through=observed)
    return manifest


def test_a_snapshot_sequence_may_advance_freshness() -> None:
    validate_reporting_revision_sequence([_snapshot_at(0), _snapshot_at(30), _snapshot_at(60)])


def test_a_snapshot_may_not_regress_freshness() -> None:
    with pytest.raises(ReportingSourceConformanceError) as error:
        validate_reporting_revision_sequence([_snapshot_at(60), _snapshot_at(0)])
    assert error.value.code == "REVISION_SEQUENCE_INVALID"


def test_a_snapshot_may_not_follow_the_official_publication() -> None:
    official_request = redacted_authoritative_request()
    _, official, _ = redacted_completed_result(official_request)
    with pytest.raises(ReportingSourceConformanceError) as error:
        validate_reporting_revision_sequence([official, _snapshot_at(0)], allow_cross_finality=True)
    assert error.value.code == "REVISION_SEQUENCE_INVALID"


def test_only_one_initial_official_publication_is_allowed() -> None:
    first_request = redacted_authoritative_request(source_execution_key="official-exec-001")
    second_request = redacted_authoritative_request(source_execution_key="official-exec-002")
    _, first, _ = redacted_completed_result(first_request)
    _, second, _ = redacted_completed_result(second_request)
    with pytest.raises(ReportingSourceConformanceError, match="only one initial authoritative"):
        validate_reporting_revision_sequence([first, second])


def test_a_correction_must_name_the_publication_it_supersedes() -> None:
    official_request = redacted_authoritative_request(source_execution_key="official-exec-001")
    _, official, _ = redacted_completed_result(official_request)
    correction_request = redacted_authoritative_request(
        source_execution_key="official-exec-002", supersedes_publication_id=official.publication_id
    )
    _, correction, _ = redacted_completed_result(correction_request)
    validate_reporting_revision_sequence([official, correction])

    wrong_request = redacted_authoritative_request(
        source_execution_key="official-exec-003", supersedes_publication_id="src-someone-else"
    )
    _, wrong, _ = redacted_completed_result(wrong_request)
    with pytest.raises(ReportingSourceConformanceError, match="exact publication it supersedes"):
        validate_reporting_revision_sequence([official, wrong])


def test_mixing_finalities_requires_an_explicit_cross_finality_decision() -> None:
    official_request = redacted_authoritative_request()
    _, official, _ = redacted_completed_result(official_request)
    with pytest.raises(ReportingSourceConformanceError, match="cross-finality"):
        validate_reporting_revision_sequence([_snapshot_at(0), official])


def test_a_sequence_must_describe_one_logical_window() -> None:
    other = redacted_snapshot_request()
    shifted = other.model_copy(
        update={"identity": other.identity.model_copy(update={"account_id": "other-account"})}
    )
    _, first, _ = redacted_completed_result(other)
    _, second, _ = redacted_completed_result(shifted)
    with pytest.raises(ReportingSourceConformanceError, match="same account"):
        validate_reporting_revision_sequence([first, second])


def test_a_later_revision_may_not_degrade_a_committed_cell() -> None:
    fresh = _snapshot_at(0)
    request = redacted_snapshot_request(
        source_execution_key="snapshot-exec-9999",
        source_read_cutoff_at=fresh.observed_at + timedelta(minutes=30),
    )
    partial = request.model_copy(
        update={"coverage": request.coverage.model_copy(update={"expected": "partial"})}
    )
    _, degraded, _ = redacted_completed_result(
        partial,
        page_bodies=[""],
        row_counts=[0],
        constituent_status="missing",
        observed_at=fresh.observed_at + timedelta(minutes=30),
        data_through=fresh.data_through,
    )
    with pytest.raises(ReportingSourceConformanceError, match="degrade"):
        validate_reporting_revision_sequence([fresh, degraded])


# -- strictness -------------------------------------------------------------


def test_a_basic_manifest_must_not_carry_fabricated_call_evidence() -> None:
    from adcp.reporting.source import ReportingProviderPageV1, SourceBatchEvidenceV1

    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request)
    evidence = SourceBatchEvidenceV1(
        pagination_kind="none",
        pagination_termination="single_page",
        pages=[
            ReportingProviderPageV1(
                ordinal=0,
                request_id="req-1",
                request_parameters_sha256="d" * 64,
                response_sha256="e" * 64,
                item_count=manifest.row_count,
                output_object_ordinals=[0],
            )
        ],
        request_count=1,
    )
    with pytest.raises(ValidationError, match="basic manifest must omit"):
        _rebind(manifest, evidence=evidence)


def test_an_evidenced_manifest_requires_call_evidence() -> None:
    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request)
    with pytest.raises(ValidationError, match="must retain per-call provider evidence"):
        _rebind(manifest, strictness="evidenced")


def test_evidenced_page_counts_must_equal_the_normalized_row_count() -> None:
    from adcp.reporting.source import ReportingProviderPageV1, SourceBatchEvidenceV1

    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request)
    evidence = SourceBatchEvidenceV1(
        pagination_kind="none",
        pagination_termination="single_page",
        pages=[
            ReportingProviderPageV1(
                ordinal=0,
                request_id="req-1",
                request_parameters_sha256="d" * 64,
                response_sha256="e" * 64,
                item_count=manifest.row_count + 5,
                output_object_ordinals=[0],
            )
        ],
        request_count=1,
    )
    with pytest.raises(ValidationError, match="must equal the normalized row count"):
        _rebind(manifest, strictness="evidenced", evidence=evidence)


def test_evidenced_request_count_must_equal_its_evidence() -> None:
    from adcp.reporting.source import ReportingProviderPageV1, SourceBatchEvidenceV1

    with pytest.raises(ValidationError, match="request_count must exactly equal"):
        SourceBatchEvidenceV1(
            pagination_kind="none",
            pagination_termination="single_page",
            pages=[
                ReportingProviderPageV1(
                    ordinal=0,
                    request_id="req-1",
                    request_parameters_sha256="d" * 64,
                    response_sha256="e" * 64,
                    item_count=0,
                    output_object_ordinals=[0],
                )
            ],
            request_count=7,
        )


def test_cursor_pagination_must_form_an_unbroken_chain() -> None:
    from adcp.reporting.source import ReportingProviderPageV1, SourceBatchEvidenceV1

    def page(ordinal: int, request_cursor: str | None, next_cursor: str | None):
        return ReportingProviderPageV1(
            ordinal=ordinal,
            request_id=f"req-{ordinal}",
            request_parameters_sha256="d" * 64,
            request_cursor=request_cursor,
            next_cursor=next_cursor,
            response_sha256="e" * 64,
            item_count=0,
            output_object_ordinals=[ordinal],
        )

    with pytest.raises(ValidationError, match="unbroken chain"):
        SourceBatchEvidenceV1(
            pagination_kind="cursor",
            pagination_termination="no_next_cursor",
            pages=[page(0, None, "c1"), page(1, "c2", None)],
            request_count=2,
        )


# -- capability integrity ---------------------------------------------------


def test_capability_checksum_binds_the_declaration() -> None:
    capabilities = redacted_capabilities()
    payload = capabilities.model_dump(mode="json")
    payload["capability_version"] = "tampered"
    with pytest.raises(ValidationError, match="must bind the complete scoped declaration"):
        ReportingSourceCapabilitiesV1.model_validate(payload)


def test_capabilities_reject_duplicate_offering_ids() -> None:
    capabilities = redacted_capabilities()
    payload = capabilities.model_dump(mode="json")
    payload["offerings"][1]["offering_id"] = payload["offerings"][0]["offering_id"]
    payload.pop("capabilities_sha256")
    from adcp.reporting.source import reporting_source_capabilities_sha256_v1

    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
    with pytest.raises(ValidationError, match="offering_id must be unique"):
        ReportingSourceCapabilitiesV1.model_validate(payload)


def test_contract_uris_must_be_publicly_resolvable() -> None:
    from adcp.reporting.source import ReportingContractIdentityV1

    with pytest.raises(ValidationError, match="public HTTPS hostname"):
        ReportingContractIdentityV1(
            report_definition_id="X",
            report_definition_uri="http://localhost:8080/definition",
            report_definition_sha256="a" * 64,
            reporting_profile="p",
            schema_version="1",
            schema_uri="https://contracts.example.test/schema",
            schema_sha256="b" * 64,
        )


# -- request coherence ------------------------------------------------------


def test_snapshot_class_and_snapshot_revision_kind_travel_together() -> None:
    request = redacted_snapshot_request()
    with pytest.raises(ValidationError, match="distinct revision kinds|snapshot offerings"):
        request.model_copy(update={"revision_kind": "authoritative"}).model_validate(
            request.model_dump(mode="json") | {"revision_kind": "authoritative"}
        )


def test_a_correction_request_must_name_its_predecessor() -> None:
    request = redacted_authoritative_request()
    payload = request.model_dump(mode="json")
    payload["revision_kind"] = "correction"
    with pytest.raises(ValidationError, match="supersedes_publication_id is required"):
        ReportingSourceSliceRequestV1.model_validate(payload)


def test_the_cell_matrix_is_bounded() -> None:
    base = redacted_snapshot_request()
    payload = base.model_dump(mode="json")
    payload["requested_metrics"] = [f"metric_{index}" for index in range(1_001)]
    with pytest.raises(ValidationError):
        ReportingSourceSliceRequestV1.model_validate(payload)


def test_source_scope_must_be_canonically_encodable() -> None:
    from adcp.reporting.source import ReportingSourceIdentityV1

    with pytest.raises(ValidationError):
        ReportingSourceIdentityV1(
            account_id="a",
            delivery_config_id="c",
            delivery_config_version=1,
            report_definition_id="D",
            reporting_obligation_id="o",
            period_key="p",
            source_execution_key="exec-0001",
            run_id="r",
            logical_slice_fingerprint=f"sha256:{'a' * 64}",
            source_scope={"rate": 1.5},
        )


def test_manifest_objects_bind_their_own_digest() -> None:
    request = redacted_snapshot_request()
    _, manifest, _ = redacted_completed_result(request)
    tampered = [manifest.objects[0].model_copy(update={"sha256": hashlib.sha256(b"x").hexdigest()})]
    with pytest.raises(ValidationError, match="object_set_sha256"):
        _rebind(manifest, objects=tampered)
