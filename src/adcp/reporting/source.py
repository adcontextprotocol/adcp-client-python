"""The seller-side reporting **source** contract: one frozen slice in, one immutable manifest out.

This is the transport-independent seam between the part of a seller that
*schedules* reporting (obligations, cadence, retry, gap detection -- the
producer in :mod:`adcp.reporting.ledger`) and the part that *fetches* it from
whatever actually holds the numbers: an ad server report API, a stats cache, a
warehouse, a partner feed.

Who owns what
-------------

The **producer** owns obligations, schedules, cadence, deduplication, gap
detection, retry and backfill policy, durable checkpoints, manifest acceptance,
revision identity, and status.  It is the scheduler.

An **executor** owns exactly one thing: fulfilling one bounded, frozen slice.
It owns its own pagination, its own async-job polling, its own retry
classification, and it returns one conforming immutable manifest.  It never
advances a durable checkpoint and never commits a revision.  An executor that
loops past its deadline, ignores cancellation, or publishes a partial result as
complete is not conforming -- see :mod:`adcp.reporting.conformance`, which says
so executably.

Two publication classes
-----------------------

``PROVISIONAL_SNAPSHOT`` and ``AUTHORITATIVE`` are *different offerings*, even
when they cover the same media buy and window. A declared settling policy may
use them sequentially for one ledger obligation: a snapshot is replaceable
current-state evidence; an authoritative publication is official-close
evidence whose later corrections are explicit, immutable adjustments. Never
relabel a later authoritative fetch as a fresh first official publication.

Two manifest strictness levels
------------------------------

``basic``
    Identity, staged objects with SHA-256, ``data_through``, coverage,
    finality evidence, and completeness.  This is what a seller wrapping an
    existing delivery read can honestly produce, and it is enough to commit a
    revision.  :mod:`adcp.reporting.inline_source` emits exactly this.

``evidenced``
    Everything in ``basic`` plus per-call provider evidence: every result page,
    every async job submission and poll, every retry attempt, and a request
    count that must *exactly* equal that evidence.  Required when a manifest
    has to prove to a third party that no provider page was silently dropped.

A ``basic`` manifest is not a weaker claim about the *data*; it is a weaker
claim about the *acquisition*.  Both bind their bytes the same way.

Generic identity
----------------

Unlike the internal platform contract this is ported from, identity here is
AdCP-shaped and vendor-neutral: account, delivery config, report definition,
period, and obligation.  Everything a particular source needs to identify
itself -- provider name, credential binding generation, ad-server network code
-- goes in one opaque :attr:`ReportingSourceIdentityV1.source_scope` mapping
that the SDK fingerprints but never interprets.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Annotated, Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from adcp.reporting.canonical_json import (
    canonical_json_sha256_v1,
    canonical_json_utf8_v1,
    reporting_fingerprint_v1,
    strip_absent,
)

__all__ = [
    "REPORTING_EVIDENCE_REASON_MAX_LENGTH_V1",
    "REPORTING_SOURCE_CONTRACT_VERSION_V1",
    "SOURCE_BATCH_MANIFEST_MAX_BYTES_V1",
    "SOURCE_BATCH_MANIFEST_MAX_METRIC_AVAILABILITY_V1",
    "SOURCE_BATCH_MANIFEST_MAX_PROVIDER_CALLS_V1",
    "AuthoritativeOfferingV1",
    "MediaBuyConstituentV1",
    "PackageItemConstituentV1",
    "ProductConstituentV1",
    "ProvisionalSnapshotOfferingV1",
    "ReportingAdapterBuildIdentityV1",
    "ReportingAvailabilityStatus",
    "ReportingConstituent",
    "ReportingConstituentCoverageV1",
    "ReportingContractIdentityV1",
    "ReportingCoverageStatus",
    "ReportingFinalityEvidenceV1",
    "ReportingManifestStrictness",
    "ReportingMetricAvailabilityV1",
    "ReportingProviderJobPollV1",
    "ReportingProviderJobV1",
    "ReportingProviderRetryAttemptV1",
    "ReportingPublicationClass",
    "ReportingRevisionKind",
    "ReportingSourceCapabilitiesV1",
    "ReportingSourceError",
    "ReportingSourceErrorCode",
    "ReportingSourceErrorV1",
    "ReportingSourceExecutionResponseV1",
    "ReportingSourceExecutor",
    "ReportingSourceExecutorResult",
    "ReportingSourceIdentityV1",
    "ReportingSourceOffering",
    "ReportingSourcePeriodV1",
    "ReportingSourceRetryClass",
    "ReportingSourceSliceRequestV1",
    "ReportingSourceStagedObjectReader",
    "ReportingSourceWindowingV1",
    "ReportingTriggerKind",
    "SourceBatchManifestReferenceV1",
    "SourceBatchManifestV1",
    "SourceBatchObjectV1",
    "SourceControlTotalV1",
    "completed_reporting_source_response_v1",
    "coverage_denominator_fingerprint_v1",
    "deterministic_source_publication_id_v1",
    "encode_source_batch_manifest_v1",
    "publication_content_fingerprint_v1",
    "reporting_source_capabilities_sha256_v1",
    "source_batch_coverage_fingerprint_v1",
    "source_batch_manifest_reference_v1",
    "source_batch_object_set_sha256_v1",
]

REPORTING_SOURCE_CONTRACT_VERSION_V1: Final[Literal["1.0"]] = "1.0"

#: A retained manifest is evidence, not a payload.  Rows belong in staged
#: objects; the manifest that binds them stays small enough to keep forever.
SOURCE_BATCH_MANIFEST_MAX_BYTES_V1 = 1_048_576

#: One availability record per accepted ``(constituent, requested metric)``
#: cell, so a slice's cell matrix is bounded by the same number.
SOURCE_BATCH_MANIFEST_MAX_METRIC_AVAILABILITY_V1 = 1_000

#: Successful calls plus retries, per slice, before publication.
SOURCE_BATCH_MANIFEST_MAX_PROVIDER_CALLS_V1 = 10_000

#: Evidence reasons are bounded ASCII so the budget is identical in Python,
#: TypeScript, and generated JSON Schema.  Provider-native or multilingual
#: diagnostic detail belongs in redacted observability, not in an immutable
#: manifest cell.
REPORTING_EVIDENCE_REASON_MAX_LENGTH_V1 = 512

_EVIDENCE_REASON_PATTERN = (
    r"^[A-Za-z0-9](?:[A-Za-z0-9 !#$%&'()*+,./:;<=>?@^_{}|~-]{0,510}[A-Za-z0-9])?$"
)
_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_FINGERPRINT_PATTERN = r"^sha256:[a-f0-9]{64}$"
_OPAQUE_REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:-]{1,255}$"
_NAMESPACE_PATTERN = r"^[a-z][a-z0-9_.:-]{0,127}$"
_METRIC_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
_GRAIN_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
_EXECUTION_KEY_PATTERN = r"^[A-Za-z0-9_.:-]{8,255}$"
_ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_INTEGER_TOTAL_PATTERN = r"^-?(?:0|[1-9][0-9]*)$"
_DECIMAL_TOTAL_PATTERN = r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"
_ISO_DURATION_RE = re.compile(
    r"^P(?=\d|T\d)(?:(\d{1,8})D)?(?:T(?=\d)(?:(\d{1,7})H)?(?:(\d{1,7})M)?"
    r"(?:(\d{1,7})(?:\.(\d{1,3}))?S)?)?$"
)

Sha256 = Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
Fingerprint = Annotated[str, StringConstraints(pattern=_FINGERPRINT_PATTERN)]
OpaqueReference = Annotated[str, StringConstraints(pattern=_OPAQUE_REFERENCE_PATTERN)]
Identifier = Annotated[str, StringConstraints(pattern=_IDENTIFIER_PATTERN)]
ExternalId = Annotated[str, StringConstraints(min_length=1, max_length=255)]
MetricName = Annotated[str, StringConstraints(pattern=_METRIC_NAME_PATTERN)]
Grain = Annotated[str, StringConstraints(pattern=_GRAIN_PATTERN)]
EvidenceReason = Annotated[str, StringConstraints(pattern=_EVIDENCE_REASON_PATTERN)]
IsoDuration = Annotated[str, StringConstraints(max_length=64)]

ReportingPublicationClass = Literal["PROVISIONAL_SNAPSHOT", "AUTHORITATIVE"]
ReportingRevisionKind = Literal["snapshot", "authoritative", "correction"]
ReportingManifestStrictness = Literal["basic", "evidenced"]
ReportingAvailabilityStatus = Literal[
    "present", "explicit_zero", "unsupported", "delayed", "partial", "stale", "missing"
]
ReportingCoverageStatus = Literal["full", "partial", "none"]
ReportingTriggerKind = Literal[
    "scheduled_poll", "webhook_hint", "retry", "backfill", "gap_recovery", "manual_replay"
]
ReportingSourceRetryClass = Literal["retryable", "terminal", "cancelled"]
ReportingSourceErrorCode = Literal[
    "AUTHENTICATION_FAILED",
    "AUTHORIZATION_FAILED",
    "INVALID_REQUEST",
    "UNSUPPORTED_OFFERING",
    "RATE_LIMITED",
    "QUOTA_EXHAUSTED",
    "PROVIDER_TRANSIENT",
    "PROVIDER_PERMANENT",
    "PROVIDER_JOB_FAILED",
    "PARTIAL_RESULT",
    "CANCELLED",
    "DEADLINE_EXCEEDED",
    "STAGING_FAILED",
    "INTEGRITY_FAILED",
]

#: Which retry classifications each error code may legally carry.  A source
#: that reports ``RATE_LIMITED`` as ``terminal`` has told the producer to stop
#: retrying something that would have succeeded; a source that reports
#: ``AUTHENTICATION_FAILED`` as ``retryable`` has told it to hammer a broken
#: credential.  Both are refused at construction.
_PERMITTED_RETRY_CLASSES: Mapping[str, frozenset[str]] = {
    "AUTHENTICATION_FAILED": frozenset({"terminal"}),
    "AUTHORIZATION_FAILED": frozenset({"terminal"}),
    "INVALID_REQUEST": frozenset({"terminal"}),
    "UNSUPPORTED_OFFERING": frozenset({"terminal"}),
    "RATE_LIMITED": frozenset({"retryable"}),
    "QUOTA_EXHAUSTED": frozenset({"retryable", "terminal"}),
    "PROVIDER_TRANSIENT": frozenset({"retryable"}),
    "PROVIDER_PERMANENT": frozenset({"terminal"}),
    "PROVIDER_JOB_FAILED": frozenset({"retryable", "terminal"}),
    "PARTIAL_RESULT": frozenset({"retryable", "terminal"}),
    "CANCELLED": frozenset({"cancelled"}),
    "DEADLINE_EXCEEDED": frozenset({"retryable", "terminal"}),
    "STAGING_FAILED": frozenset({"retryable", "terminal"}),
    "INTEGRITY_FAILED": frozenset({"terminal"}),
}

_BACKOFF_EVIDENCE_CODES = frozenset({"RATE_LIMITED", "QUOTA_EXHAUSTED", "PROVIDER_TRANSIENT"})

_AVAILABLE_STATUSES = frozenset({"present", "explicit_zero"})

#: A constituent's roll-up status constrains what its metric cells may claim.
#: ``partial`` is the only genuinely mixed roll-up: its cells keep independent
#: statuses precisely so a consumer can select a compatible subset.
_CONSTITUENT_ALLOWS_METRIC: Mapping[str, frozenset[str]] = {
    "present": frozenset({"present", "explicit_zero"}),
    "explicit_zero": frozenset({"explicit_zero"}),
    "unsupported": frozenset({"unsupported"}),
    "delayed": frozenset({"unsupported", "delayed", "missing"}),
    "stale": frozenset({"unsupported", "delayed", "stale", "missing"}),
    "missing": frozenset({"missing"}),
    "partial": frozenset(
        {"present", "explicit_zero", "unsupported", "delayed", "partial", "stale", "missing"}
    ),
}


def _instant(value: datetime) -> str:
    """Serialize an instant the way the manifest binds it.

    Millisecond precision, UTC, ``Z`` suffix -- the exact precision used for
    every period, freshness, cutoff, and event-range comparison, so two
    producers cannot disagree about whether ``dataThrough`` reached the period
    end.
    """
    moment = value.astimezone(timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=(value.microsecond // 1000) * 1000)


def iso_duration_milliseconds_v1(value: str) -> int:
    """Exact milliseconds for the restricted ISO-8601 duration profile.

    Only day/hour/minute/second components with at most millisecond precision;
    months and years are deliberately absent because their length depends on a
    calendar the source and the producer may not share.
    """
    match = _ISO_DURATION_RE.match(value)
    if match is None:
        raise ValueError(f"{value!r} is not a supported reporting duration")
    days, hours, minutes, seconds, fraction = (
        int(match.group(1) or 0),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
        int(match.group(4) or 0),
        (match.group(5) or ""),
    )
    milliseconds = int(fraction.ljust(3, "0")[:3] or 0)
    return ((days * 86_400 + hours * 3_600 + minutes * 60 + seconds) * 1_000) + milliseconds


class _Frozen(BaseModel):
    """Reporting evidence is immutable once published; so are its models."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def canonical_payload(self) -> dict[str, Any]:
        """The ``canonical_json_utf8_v1``-ready mapping for this model.

        Absent optional fields are dropped rather than emitted as ``null``:
        ``canonical_json_utf8_v1`` has no representation for "undefined", and a
        field that is present-and-null is a different logical document from one
        that is absent.
        """
        payload = self.model_dump(mode="json", exclude_none=True, by_alias=True)
        return dict(payload)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class ReportingSourceIdentityV1(_Frozen):
    """The frozen identity of one slice execution.

    ``source_execution_key`` is the idempotency key: reusing it MUST return the
    byte-identical manifest and the same staged object set.  ``run_id`` may
    change across retries of the same logical work; ``source_execution_key``
    may not.

    ``source_scope`` is the deliberate escape hatch.  Everything a particular
    reporting source needs to pin itself -- provider name, ad-server network
    code, credential binding generation, region -- lives there as opaque JSON.
    The SDK fingerprints it into the logical slice identity (so a re-bound
    account produces a different slice, not a silent continuation of the old
    one) but never reads a key out of it.
    """

    account_id: ExternalId
    delivery_config_id: ExternalId
    delivery_config_version: int = Field(ge=1)
    report_definition_id: Identifier
    reporting_obligation_id: ExternalId
    period_key: Identifier
    source_execution_key: Annotated[str, StringConstraints(pattern=_EXECUTION_KEY_PATTERN)]
    run_id: ExternalId
    logical_slice_fingerprint: Fingerprint
    source_scope: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _source_scope_is_canonical(self) -> ReportingSourceIdentityV1:
        # Fail at construction rather than at publication: a source_scope
        # carrying a float or a non-string key cannot be fingerprinted, and
        # discovering that while sealing a manifest loses the fetch.
        canonical_json_utf8_v1(self.source_scope)
        return self


class ReportingContractIdentityV1(_Frozen):
    """The content-addressed report definition this slice was fetched against."""

    report_definition_id: Identifier
    report_definition_uri: Annotated[str, StringConstraints(max_length=2_048)]
    report_definition_sha256: Sha256
    reporting_profile: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
    schema_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    schema_uri: Annotated[str, StringConstraints(max_length=2_048)]
    schema_sha256: Sha256
    schema_dialect: Literal["https://json-schema.org/draft/2020-12/schema"] = (
        "https://json-schema.org/draft/2020-12/schema"
    )
    schema_ref_policy: Literal["local_fragment_only"] = "local_fragment_only"

    @model_validator(mode="after")
    def _uris_are_public_https(self) -> ReportingContractIdentityV1:
        for field, value in (
            ("report_definition_uri", self.report_definition_uri),
            ("schema_uri", self.schema_uri),
        ):
            _require_public_https(field, value)
        return self


def _require_public_https(field: str, value: str) -> None:
    """Refuse contract URIs that cannot be resolved by both parties.

    A contract URI is retained evidence a counterparty is expected to fetch
    years later.  ``http://``, embedded credentials, ``localhost``, and bare IP
    literals all describe something only the publisher can reach, which makes
    the "content-addressed" claim unverifiable.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    hostname = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or not hostname
        or hostname == "localhost"
        or "." not in hostname
        or re.fullmatch(r"\d+(?:\.\d+){3}", hostname)
    ):
        raise ValueError(f"{field} must use a public HTTPS hostname, got {value!r}")


class ReportingAdapterBuildIdentityV1(_Frozen):
    """Which build of which executor produced a manifest.

    Pinned in the manifest so a bug traced to one adapter release can be scoped
    to exactly the publications that release produced.
    """

    executor_id: Identifier
    adapter_id: Identifier
    adapter_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    adapter_build_sha256: Sha256


# --------------------------------------------------------------------------
# Period and windowing
# --------------------------------------------------------------------------


class ReportingSourceWindowingV1(_Frozen):
    """How a source's reporting window behaves."""

    kind: Literal["cumulative_current_window", "fixed_closed_window", "provider_defined"]
    minimum_window: IsoDuration
    maximum_window: IsoDuration
    overlapping_windows_supported: bool = False

    @model_validator(mode="after")
    def _window_range_is_ordered(self) -> ReportingSourceWindowingV1:
        if iso_duration_milliseconds_v1(self.minimum_window) > iso_duration_milliseconds_v1(
            self.maximum_window
        ):
            raise ValueError("maximum_window must be at least minimum_window")
        return self


class ReportingSourcePeriodV1(_Frozen):
    """The half-open reporting window, frozen before dispatch.

    Every field here is part of admission identity and MUST be echoed exactly
    by the manifest.  The request deliberately does *not* predict
    ``observed_at`` or ``data_through`` -- those are results the source
    reports, not instructions the producer gives.
    """

    period_key: Identifier
    source_local_date: Annotated[str, StringConstraints(pattern=_ISO_DATE_PATTERN)]
    start: datetime
    end: datetime
    source_timezone: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    source_read_cutoff_at: datetime
    grain: Grain
    windowing: ReportingSourceWindowingV1

    @model_validator(mode="after")
    def _period_is_coherent(self) -> ReportingSourcePeriodV1:
        start = _aware(self.start, "start")
        end = _aware(self.end, "end")
        cutoff = _aware(self.source_read_cutoff_at, "source_read_cutoff_at")
        if end <= start:
            raise ValueError("period must be nonempty (end must follow start)")
        if cutoff < start:
            raise ValueError("source_read_cutoff_at must not precede the bounded period")
        _require_iana_timezone(self.source_timezone)
        if _source_local_date(start, self.source_timezone) != self.source_local_date:
            raise ValueError("source_local_date must be the source-local date at period start")
        return self


def _require_iana_timezone(name: str) -> None:
    if name.startswith(("+", "-")):
        raise ValueError(f"source_timezone must be an IANA zone name, got {name!r}")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
    except Exception as error:  # pragma: no cover - platform tzdata differences
        raise ValueError(f"source_timezone must be a valid IANA timezone, got {name!r}") from error


def _source_local_date(instant: datetime, timezone_name: str) -> str:
    from zoneinfo import ZoneInfo

    return instant.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Coverage constituents
# --------------------------------------------------------------------------


class ProductConstituentV1(_Frozen):
    """A product-level constituent: no package or media-buy parent."""

    constituent_kind: Literal["product"] = "product"
    constituent_id: ExternalId
    product_id: ExternalId


class PackageItemConstituentV1(_Frozen):
    """A package-item constituent: a product inside a named package."""

    constituent_kind: Literal["package_item"] = "package_item"
    constituent_id: ExternalId
    product_id: ExternalId
    package_id: ExternalId


class MediaBuyConstituentV1(_Frozen):
    """A media-buy constituent: a product inside a named media buy."""

    constituent_kind: Literal["media_buy"] = "media_buy"
    constituent_id: ExternalId
    product_id: ExternalId
    media_buy_id: ExternalId


ReportingConstituent = Annotated[
    ProductConstituentV1 | PackageItemConstituentV1 | MediaBuyConstituentV1,
    Field(discriminator="constituent_kind"),
]


def coverage_denominator_fingerprint_v1(constituents: Sequence[Any]) -> str:
    """Fingerprint the *requested* constituent set, excluding publication status.

    This is what makes "did I ask for the same thing?" answerable across a
    request and its manifest without comparing two differently shaped
    documents.  Sorting by ``constituent_id`` means request order is not part
    of identity; adding, removing, or re-binding a constituent changes it.
    """
    ordered = sorted(
        (
            {
                key: value
                for key, value in _constituent_identity(constituent).items()
                if value is not None
            }
            for constituent in constituents
        ),
        key=lambda item: str(item["constituent_id"]).encode("utf-16-be", errors="surrogatepass"),
    )
    return reporting_fingerprint_v1(
        {"kind": "reporting_coverage_denominator_v1", "constituents": ordered}
    )


def _constituent_identity(constituent: Any) -> dict[str, Any]:
    """The identity half of a constituent, with mutable publication status dropped."""
    if isinstance(constituent, BaseModel):
        payload = constituent.model_dump(mode="json", exclude_none=True)
    else:
        payload = dict(constituent)
    return {
        key: payload.get(key)
        for key in (
            "constituent_id",
            "constituent_kind",
            "product_id",
            "package_id",
            "media_buy_id",
        )
        if payload.get(key) is not None
    }


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


class MetricOfferingV1(_Frozen):
    """One metric a source offers, bound to its versioned semantic contract."""

    name: MetricName
    support: Literal["exact", "partial", "unavailable"] = "exact"
    semantic_contract_id: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,255}$")
    ]
    semantic_contract_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    semantic_contract_sha256: Sha256
    reason: EvidenceReason | None = None

    @model_validator(mode="after")
    def _degraded_support_is_explained(self) -> MetricOfferingV1:
        if self.support != "exact" and not self.reason:
            raise ValueError("partial and unavailable metric support needs a stable reason")
        return self


class DimensionOfferingV1(_Frozen):
    """One dimension a source offers."""

    name: MetricName
    support: Literal["exact", "partial", "unavailable"] = "exact"
    reason: EvidenceReason | None = None

    @model_validator(mode="after")
    def _degraded_support_is_explained(self) -> DimensionOfferingV1:
        if self.support != "exact" and not self.reason:
            raise ValueError("partial and unavailable dimension support needs a stable reason")
        return self


class SourceFormatV1(_Frozen):
    """A staged-object encoding this source can produce."""

    media_type: Literal[
        "application/json",
        "application/x-ndjson",
        "text/csv",
        "application/vnd.apache.parquet",
    ]
    compression: Literal["none", "gzip", "zstd"] = "none"


class ProviderExecutionV1(_Frozen):
    """How the source's upstream behaves, so the producer can size a slice."""

    pagination: Literal["none", "cursor", "page", "offset"] = "none"
    async_jobs: Literal["unsupported", "optional", "required"] = "unsupported"
    maximum_window_days_per_request: int = Field(ge=1, le=104_249)
    supports_cancellation: bool = False
    rate_limit_constraints: list[
        Annotated[str, StringConstraints(min_length=1, max_length=512)]
    ] = Field(default_factory=list, max_length=100)


class _OfferingBase(_Frozen):
    offering_id: Identifier
    publication_namespace: Annotated[str, StringConstraints(pattern=_NAMESPACE_PATTERN)]
    product_ids: list[ExternalId] = Field(min_length=1, max_length=10_000)
    constituent_kinds: list[Literal["product", "package_item", "media_buy"]] = Field(
        min_length=1, max_length=3
    )
    contract: ReportingContractIdentityV1
    source_timezone: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    grain: Grain
    windowing: ReportingSourceWindowingV1
    metrics: list[MetricOfferingV1] = Field(min_length=1, max_length=1_000)
    dimensions: list[DimensionOfferingV1] = Field(default_factory=list, max_length=1_000)
    formats: list[SourceFormatV1] = Field(min_length=1, max_length=16)
    provider_execution: ProviderExecutionV1
    manifest_strictness: ReportingManifestStrictness = "basic"
    retention_days: int = Field(ge=1)

    @model_validator(mode="after")
    def _names_are_unique(self) -> _OfferingBase:
        for label, values in (
            ("metrics", [metric.name for metric in self.metrics]),
            ("dimensions", [dimension.name for dimension in self.dimensions]),
            ("product_ids", self.product_ids),
            ("constituent_kinds", self.constituent_kinds),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} must be unique within an offering")
        if self.source_timezone is not None:
            _require_iana_timezone(self.source_timezone)
        return self


class ProvisionalSnapshotOfferingV1(_OfferingBase):
    """Fast, replaceable current-state evidence.

    ``fastest_safe_cadence`` is a *lower bound the producer may choose*, not an
    SLA and not an invitation to poll faster.  Declaring a 15-minute cadence
    without upstream evidence that the account tolerates it is how a source
    gets itself rate-limited into ``action_required``.

    ``restatement_window`` opts into automatic re-reads after period close.
    ``restatement_cadence`` defaults to the configured reporting period (and
    is never allowed to beat ``fastest_safe_cadence``). ``official_close_lag``
    asks a producer that also has an authoritative offering to publish a
    terminal official revision after the source settles. Omitting the window
    preserves the original one-shot behavior.
    """

    publication_class: Literal["PROVISIONAL_SNAPSHOT"] = "PROVISIONAL_SNAPSHOT"
    fastest_safe_cadence: IsoDuration
    alignment: Literal["source_timezone", "provider_defined", "unaligned"] = "provider_defined"
    expected_availability_lag: IsoDuration
    worst_case_availability_lag: IsoDuration
    revision_semantics: Literal["provisional_replaceable"] = "provisional_replaceable"
    restatement_window: IsoDuration | None = None
    restatement_cadence: IsoDuration | None = None
    official_close_lag: IsoDuration | None = None

    @model_validator(mode="after")
    def _lag_range_is_ordered(self) -> ProvisionalSnapshotOfferingV1:
        _validate_lag_range(self.expected_availability_lag, self.worst_case_availability_lag)
        if self.restatement_window is None:
            if self.restatement_cadence is not None or self.official_close_lag is not None:
                raise ValueError(
                    "restatement_cadence and official_close_lag require restatement_window"
                )
            return self

        iso_duration_milliseconds_v1(self.restatement_window)
        if self.restatement_cadence is not None:
            cadence_ms = iso_duration_milliseconds_v1(self.restatement_cadence)
            if cadence_ms <= 0:
                raise ValueError("restatement_cadence must be greater than zero")
            if cadence_ms < iso_duration_milliseconds_v1(self.fastest_safe_cadence):
                raise ValueError("restatement_cadence must not be faster than fastest_safe_cadence")
        if self.official_close_lag is not None:
            iso_duration_milliseconds_v1(self.official_close_lag)
        return self


class AuthoritativeOfferingV1(_OfferingBase):
    """Official-close evidence, with a declared correction policy.

    The first publication is ``authoritative``.  A later restatement is a
    ``correction`` that names the exact publication it adjusts -- never a
    second "first official" publication.
    """

    publication_class: Literal["AUTHORITATIVE"] = "AUTHORITATIVE"
    source_local_ready_time: Annotated[
        str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    ]
    days_after_period_end: int = Field(ge=0)
    expected_availability_lag: IsoDuration
    worst_case_availability_lag: IsoDuration
    correction_window: IsoDuration
    correction_policy: Literal["none", "immutable_correction"] = "none"
    revision_semantics: Literal["official_with_declared_correction_policy"] = (
        "official_with_declared_correction_policy"
    )

    @model_validator(mode="after")
    def _lag_range_is_ordered(self) -> AuthoritativeOfferingV1:
        _validate_lag_range(self.expected_availability_lag, self.worst_case_availability_lag)
        return self


def _validate_lag_range(expected: str, worst_case: str) -> None:
    if iso_duration_milliseconds_v1(expected) > iso_duration_milliseconds_v1(worst_case):
        raise ValueError("worst_case_availability_lag must be at least expected_availability_lag")


ReportingSourceOffering = Annotated[
    ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1,
    Field(discriminator="publication_class"),
]


class ReportingSourceCapabilitiesV1(_Frozen):
    """What one executor build can truthfully do, for one bound account scope.

    ``scope`` distinguishes a *product declaration* (what this adapter can do
    in general) from an *effective account* result (what these credentials can
    do for this account right now).  Only the latter is executable; the
    distinction exists so an integration-wide brochure cannot masquerade as
    account eligibility.

    ``capabilities_sha256`` binds the whole scoped declaration.  It is computed
    for you by :func:`reporting_source_capabilities_sha256_v1` and verified on
    every construction, so a declaration edited in flight fails loudly.
    """

    contract_version: Literal["1.0"] = REPORTING_SOURCE_CONTRACT_VERSION_V1
    scope: Literal["product_declaration", "effective_account"]
    adapter_build: ReportingAdapterBuildIdentityV1
    source_scope: dict[str, Any] = Field(default_factory=dict)
    capability_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    capabilities_sha256: Sha256
    offerings: list[ReportingSourceOffering] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _checksum_binds_the_declaration(self) -> ReportingSourceCapabilitiesV1:
        offering_ids = [offering.offering_id for offering in self.offerings]
        if len(set(offering_ids)) != len(offering_ids):
            raise ValueError("offering_id must be unique within a capability declaration")
        expected = reporting_source_capabilities_sha256_v1(self)
        if expected != self.capabilities_sha256:
            raise ValueError(
                "capabilities_sha256 must bind the complete scoped declaration; "
                f"expected {expected}"
            )
        return self

    def offering(self, offering_id: str) -> ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1:
        """The offering with this id, or ``KeyError``."""
        for candidate in self.offerings:
            if candidate.offering_id == offering_id:
                return candidate
        raise KeyError(offering_id)


def reporting_source_capabilities_sha256_v1(
    capabilities: ReportingSourceCapabilitiesV1 | Mapping[str, Any],
) -> str:
    """SHA-256 over the declaration with ``capabilities_sha256`` itself removed."""
    raw = (
        capabilities.model_dump(mode="json", exclude_none=True)
        if isinstance(capabilities, BaseModel)
        else dict(capabilities)
    )
    payload = dict(strip_absent(raw))
    payload.pop("capabilities_sha256", None)
    return canonical_json_sha256_v1(payload)


# --------------------------------------------------------------------------
# Slice request
# --------------------------------------------------------------------------


class ReportingSourceCoverageRequestV1(_Frozen):
    """The requested constituent denominator.

    ``expected`` is executable admission policy, not a hint.  A ``full``
    request can only complete with ``full`` manifest coverage; an executor that
    cannot deliver that returns a truthful ``PARTIAL_RESULT`` error and
    publishes nothing.
    """

    expected: Literal["full", "partial"] = "full"
    constituents: list[ReportingConstituent] = Field(
        min_length=1, max_length=SOURCE_BATCH_MANIFEST_MAX_METRIC_AVAILABILITY_V1
    )
    denominator_fingerprint: Fingerprint

    @model_validator(mode="after")
    def _denominator_binds_the_constituents(self) -> ReportingSourceCoverageRequestV1:
        ids = [constituent.constituent_id for constituent in self.constituents]
        if len(set(ids)) != len(ids):
            raise ValueError("constituent_id must be unique within a coverage denominator")
        expected = coverage_denominator_fingerprint_v1(self.constituents)
        if expected != self.denominator_fingerprint:
            raise ValueError(
                "denominator_fingerprint must bind the complete constituent tuple set; "
                f"expected {expected}"
            )
        return self


class ReportingSourceCheckpointV1(_Frozen):
    """Producer-owned cursor evidence an executor may read but never advance."""

    checkpoint_id: OpaqueReference
    cursor: Annotated[str, StringConstraints(min_length=1, max_length=2_048)] | None = None
    watermark: Annotated[str, StringConstraints(min_length=1, max_length=2_048)] | None = None
    checkpoint_sha256: Sha256


class ReportingSourceSliceRequestV1(_Frozen):
    """One bounded, frozen unit of work handed to an executor.

    Everything here is immutable for the life of the execution.  The executor
    echoes it into the manifest; the conformance validator compares the two
    field by field, so a source that "helpfully" widens a date range or swaps a
    currency fails admission rather than silently publishing something the
    producer never asked for.
    """

    contract_version: Literal["1.0"] = REPORTING_SOURCE_CONTRACT_VERSION_V1
    identity: ReportingSourceIdentityV1
    adapter_build: ReportingAdapterBuildIdentityV1
    offering_id: Identifier
    publication_namespace: Annotated[str, StringConstraints(pattern=_NAMESPACE_PATTERN)]
    publication_class: ReportingPublicationClass
    contract: ReportingContractIdentityV1
    period: ReportingSourcePeriodV1
    revision_kind: ReportingRevisionKind
    supersedes_publication_id: OpaqueReference | None = None
    trigger: ReportingTriggerKind = "scheduled_poll"
    coverage: ReportingSourceCoverageRequestV1
    requested_metrics: list[MetricName] = Field(min_length=1, max_length=1_000)
    requested_dimensions: list[MetricName] = Field(default_factory=list, max_length=1_000)
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    prior_checkpoint: ReportingSourceCheckpointV1 | None = None
    deadline_at: datetime

    @model_validator(mode="after")
    def _request_is_coherent(self) -> ReportingSourceSliceRequestV1:
        snapshot = self.publication_class == "PROVISIONAL_SNAPSHOT"
        if snapshot != (self.revision_kind == "snapshot"):
            raise ValueError(
                "snapshot offerings produce snapshot revisions; authoritative offerings do not"
            )
        if (self.revision_kind == "correction") != (self.supersedes_publication_id is not None):
            raise ValueError(
                "supersedes_publication_id is required for a correction and forbidden otherwise"
            )
        for label, values in (
            ("requested_metrics", self.requested_metrics),
            ("requested_dimensions", self.requested_dimensions),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} must be unique")
        cells = len(self.coverage.constituents) * len(self.requested_metrics)
        if cells > SOURCE_BATCH_MANIFEST_MAX_METRIC_AVAILABILITY_V1:
            raise ValueError(
                "a source slice may request at most "
                f"{SOURCE_BATCH_MANIFEST_MAX_METRIC_AVAILABILITY_V1} constituent-metric cells, "
                f"got {cells}"
            )
        _aware(self.deadline_at, "deadline_at")
        return self

    @property
    def product_ids(self) -> tuple[str, ...]:
        """The unique products represented by the constituent denominator."""
        return tuple(
            dict.fromkeys(constituent.product_id for constituent in self.coverage.constituents)
        )

    @property
    def media_buy_ids(self) -> tuple[str, ...]:
        """The unique media buys represented by the constituent denominator."""
        return tuple(
            dict.fromkeys(
                constituent.media_buy_id
                for constituent in self.coverage.constituents
                if isinstance(constituent, MediaBuyConstituentV1)
            )
        )

    @property
    def package_ids(self) -> tuple[str, ...]:
        """The unique packages represented by the constituent denominator."""
        return tuple(
            dict.fromkeys(
                constituent.package_id
                for constituent in self.coverage.constituents
                if isinstance(constituent, PackageItemConstituentV1)
            )
        )


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


class SourceBatchObjectV1(_Frozen):
    """One immutable, generation-pinned staged object holding normalized rows.

    ``object_generation`` is what makes the reference immutable: an object
    store key can be overwritten, a key plus generation cannot.  Re-reading the
    pair years later either returns the same bytes or fails -- it never returns
    different bytes.
    """

    ordinal: int = Field(ge=0)
    object_ref: OpaqueReference
    object_generation: OpaqueReference
    media_type: Literal[
        "application/json",
        "application/x-ndjson",
        "text/csv",
        "application/vnd.apache.parquet",
    ]
    compression: Literal["none", "gzip", "zstd"] = "none"
    sha256: Sha256
    byte_count: int = Field(ge=0)
    row_count: int = Field(ge=0)


class SourceControlTotalV1(_Frozen):
    """A named total a consumer can check its own ingest against.

    Decimals are strings.  A JSON float here would round differently in two
    languages and turn an equality check into a flake.
    """

    name: MetricName
    value: str
    value_type: Literal["integer", "decimal"] = "integer"
    unit: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None

    @model_validator(mode="after")
    def _value_matches_its_type(self) -> SourceControlTotalV1:
        pattern = _INTEGER_TOTAL_PATTERN if self.value_type == "integer" else _DECIMAL_TOTAL_PATTERN
        if not re.match(pattern, self.value):
            raise ValueError(f"{self.value!r} is not a valid {self.value_type} control total")
        return self

    @property
    def is_zero(self) -> bool:
        return re.fullmatch(r"-?0(?:\.0+)?", self.value) is not None


class ReportingFinalityEvidenceV1(_Frozen):
    """Why the executor believes the data is as final as it claims."""

    basis: Literal[
        "provisional_observation",
        "provider_declared",
        "provider_job_terminal",
        "elapsed_settlement_window",
    ]
    observed_at: datetime
    evidence_ref: ExternalId | None = None
    provisional_until: datetime | None = None

    @model_validator(mode="after")
    def _provisional_signal_matches_basis(self) -> ReportingFinalityEvidenceV1:
        if self.provisional_until is None:
            return self
        if self.basis != "provisional_observation":
            raise ValueError("provisional_until belongs only to provisional observations")
        if _aware(self.provisional_until, "provisional_until") < _aware(
            self.observed_at, "observed_at"
        ):
            raise ValueError("provisional_until must not predate the source observation")
        return self


class ReportingConstituentCoverageV1(_Frozen):
    """One constituent's publication evidence for this slice."""

    constituent: ReportingConstituent
    status: ReportingAvailabilityStatus
    data_through: datetime | None = None
    reason: EvidenceReason | None = None

    @model_validator(mode="after")
    def _unavailable_coverage_is_explained(self) -> ReportingConstituentCoverageV1:
        if self.status not in _AVAILABLE_STATUSES and not self.reason:
            raise ValueError(f"constituent status {self.status!r} needs a stable reason")
        return self

    @property
    def constituent_id(self) -> str:
        return self.constituent.constituent_id


class ReportingMetricAvailabilityV1(_Frozen):
    """One ``(constituent, metric)`` cell's publication evidence.

    Every accepted cell gets exactly one of these.  ``missing`` and
    ``explicit_zero`` are never interchangeable: the first says the source
    failed to answer, the second says it answered "nothing happened".
    """

    constituent_id: ExternalId
    metric: MetricName
    semantic_contract_id: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,255}$")
    ]
    semantic_contract_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    semantic_contract_sha256: Sha256
    status: ReportingAvailabilityStatus
    data_through: datetime | None = None
    reason: EvidenceReason | None = None

    @model_validator(mode="after")
    def _evidence_matches_status(self) -> ReportingMetricAvailabilityV1:
        if self.status in _AVAILABLE_STATUSES and self.data_through is None:
            raise ValueError(f"metric status {self.status!r} needs its source watermark")
        if self.status not in _AVAILABLE_STATUSES and not self.reason:
            raise ValueError(f"metric status {self.status!r} needs a stable reason")
        return self


class SourceBatchCoverageV1(_Frozen):
    """The slice's coverage roll-up plus its per-constituent evidence."""

    denominator_fingerprint: Fingerprint
    status: ReportingCoverageStatus
    constituents: list[ReportingConstituentCoverageV1] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def _status_matches_the_evidence(self) -> SourceBatchCoverageV1:
        ids = [item.constituent_id for item in self.constituents]
        if len(set(ids)) != len(ids):
            raise ValueError("constituent_id must be unique within manifest coverage")
        covered = sum(1 for item in self.constituents if item.status in _AVAILABLE_STATUSES)
        partial = sum(1 for item in self.constituents if item.status == "partial")
        all_covered = covered == len(self.constituents)
        any_evidence = covered > 0 or partial > 0
        if self.status == "full" and not all_covered:
            raise ValueError("full coverage requires every constituent present or explicit zero")
        if self.status == "none" and any_evidence:
            raise ValueError("none coverage must not contain a covered or partial constituent")
        if self.status == "partial" and (not any_evidence or all_covered):
            raise ValueError(
                "partial coverage requires coverage evidence without every constituent covered"
            )
        expected = coverage_denominator_fingerprint_v1(
            [item.constituent for item in self.constituents]
        )
        if expected != self.denominator_fingerprint:
            raise ValueError(
                "denominator_fingerprint must bind the complete constituent tuple set; "
                f"expected {expected}"
            )
        return self


class ReportingProviderPageV1(_Frozen):
    """One successful upstream result page.

    ``item_count`` must equal the normalized row count of the objects this page
    produced.  That equality is what turns "I fetched everything" from a claim
    into evidence: a dropped page shows up as a row-count mismatch.
    """

    ordinal: int = Field(ge=0)
    request_id: ExternalId
    request_parameters_sha256: Sha256
    request_cursor: Annotated[str, StringConstraints(min_length=1, max_length=2_048)] | None = None
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=2_048)] | None = None
    page_number: int | None = Field(default=None, ge=1)
    offset: int | None = Field(default=None, ge=0)
    response_sha256: Sha256
    item_count: int = Field(ge=0)
    output_object_ordinals: list[int] = Field(min_length=1, max_length=100_000)


class ReportingProviderJobV1(_Frozen):
    """One upstream async job submitted for this slice."""

    job_id: ExternalId
    submission_request_id: ExternalId
    submission_response_sha256: Sha256


class ReportingProviderJobPollV1(_Frozen):
    """One poll of an upstream async job."""

    job_id: ExternalId
    ordinal: int = Field(ge=0)
    request_id: ExternalId
    observed_at: datetime
    state: Literal["queued", "running", "succeeded"]
    response_sha256: Sha256


class ReportingProviderRetryAttemptV1(_Frozen):
    """One retried upstream call, with the classification that justified it."""

    attempt_id: OpaqueReference
    request_id: ExternalId | None = None
    attempted_at: datetime
    classification: Literal[
        "RATE_LIMITED", "QUOTA_EXHAUSTED", "PROVIDER_TRANSIENT", "PROVIDER_JOB_FAILED"
    ]


class SourceBatchEvidenceV1(_Frozen):
    """Per-call acquisition evidence: required for ``evidenced`` manifests.

    Present only when the offering promises it.  A ``basic`` manifest omits
    this whole block rather than filling it with plausible-looking placeholders
    -- an empty claim is honest, a fabricated one is not.
    """

    pagination_kind: Literal["none", "cursor", "page", "offset"] = "none"
    pagination_termination: Literal[
        "single_page", "no_next_cursor", "known_page_count_reached", "empty_terminal_page"
    ] = "single_page"
    known_page_count: int | None = Field(default=None, ge=1)
    pages: list[ReportingProviderPageV1] = Field(min_length=1, max_length=10_000)
    jobs: list[ReportingProviderJobV1] = Field(
        default_factory=list, max_length=SOURCE_BATCH_MANIFEST_MAX_PROVIDER_CALLS_V1
    )
    polls: list[ReportingProviderJobPollV1] = Field(
        default_factory=list, max_length=SOURCE_BATCH_MANIFEST_MAX_PROVIDER_CALLS_V1
    )
    retry_attempts: list[ReportingProviderRetryAttemptV1] = Field(
        default_factory=list, max_length=SOURCE_BATCH_MANIFEST_MAX_PROVIDER_CALLS_V1
    )
    request_count: int = Field(ge=0, le=SOURCE_BATCH_MANIFEST_MAX_PROVIDER_CALLS_V1)

    @model_validator(mode="after")
    def _evidence_is_internally_consistent(self) -> SourceBatchEvidenceV1:
        for index, page in enumerate(self.pages):
            if page.ordinal != index:
                raise ValueError("page evidence must be contiguous and ordered from zero")
        request_ids = [page.request_id for page in self.pages]
        request_ids += [job.submission_request_id for job in self.jobs]
        request_ids += [poll.request_id for poll in self.polls]
        request_ids += [
            attempt.request_id for attempt in self.retry_attempts if attempt.request_id is not None
        ]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("provider request ids must be unique across pages, jobs, and polls")
        self._validate_pagination()
        self._validate_jobs()
        evidenced = len(self.pages) + len(self.jobs) + len(self.polls) + len(self.retry_attempts)
        if evidenced > SOURCE_BATCH_MANIFEST_MAX_PROVIDER_CALLS_V1:
            raise ValueError("provider call evidence exceeds the bounded per-slice denominator")
        if self.request_count != evidenced:
            raise ValueError(
                "request_count must exactly equal pages + job submissions + polls + retries, "
                f"expected {evidenced}"
            )
        return self

    def _validate_pagination(self) -> None:
        kind = self.pagination_kind
        pages = self.pages
        cursor_fields_used = any(
            page.request_cursor is not None or page.next_cursor is not None for page in pages
        )
        number_fields_used = any(page.page_number is not None for page in pages)
        offset_fields_used = any(page.offset is not None for page in pages)
        if kind == "none":
            if (
                len(pages) != 1
                or self.pagination_termination != "single_page"
                or self.known_page_count is not None
                or cursor_fields_used
                or number_fields_used
                or offset_fields_used
            ):
                raise ValueError("non-paginated evidence requires exactly one terminal page")
        elif kind == "cursor":
            if number_fields_used or offset_fields_used:
                raise ValueError("cursor pagination must not carry page numbers or offsets")
            seen: set[str] = set()
            for index, page in enumerate(pages):
                if page.request_cursor is not None and page.request_cursor in seen:
                    raise ValueError("cursor pagination must not repeat or cycle through a cursor")
                if page.next_cursor is not None and (
                    page.next_cursor in seen or page.next_cursor == page.request_cursor
                ):
                    raise ValueError("cursor pagination must not repeat or cycle through a cursor")
                if index and page.request_cursor != pages[index - 1].next_cursor:
                    raise ValueError("cursor pagination must form an unbroken chain")
                if page.request_cursor is not None:
                    seen.add(page.request_cursor)
            if (
                pages[-1].next_cursor is not None
                or self.pagination_termination != "no_next_cursor"
                or self.known_page_count is not None
            ):
                raise ValueError("cursor pagination is complete only at a page with no next cursor")
        elif kind == "page":
            if cursor_fields_used or offset_fields_used:
                raise ValueError("page-number pagination must not carry cursors or offsets")
            if any(page.page_number != index + 1 for index, page in enumerate(pages)):
                raise ValueError("page-number evidence must be contiguous from the first page")
            by_count = (
                self.pagination_termination == "known_page_count_reached"
                and self.known_page_count == len(pages)
            )
            by_empty = (
                self.pagination_termination == "empty_terminal_page"
                and self.known_page_count is None
                and pages[-1].item_count == 0
            )
            if not by_count and not by_empty:
                raise ValueError(
                    "page-number pagination requires the exact known page count or an "
                    "empty terminal page"
                )
        elif kind == "offset":
            if cursor_fields_used or number_fields_used:
                raise ValueError("offset pagination must not carry cursors or page numbers")
            expected_offset = 0
            for page in pages:
                if page.offset != expected_offset:
                    raise ValueError("offset evidence must be contiguous from the first offset")
                expected_offset += page.item_count
            if (
                self.pagination_termination != "empty_terminal_page"
                or self.known_page_count is not None
                or pages[-1].item_count != 0
            ):
                raise ValueError("offset pagination requires a retained empty terminal page")

    def _validate_jobs(self) -> None:
        job_ids = [job.job_id for job in self.jobs]
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("job_id must be unique")
        known = {job_id: index for index, job_id in enumerate(job_ids)}
        counts: dict[str, int] = dict.fromkeys(job_ids, 0)
        last_observed: dict[str, datetime] = {}
        last_state: dict[str, str] = {}
        prior_index = -1
        for poll in self.polls:
            if poll.job_id not in known:
                raise ValueError("every job poll must reference a declared job")
            if known[poll.job_id] < prior_index:
                raise ValueError("job polls must be grouped in declared job order")
            prior_index = max(prior_index, known[poll.job_id])
            if poll.ordinal != counts[poll.job_id]:
                raise ValueError("job poll ordinals must be contiguous from zero per job")
            observed = _aware(poll.observed_at, "observed_at")
            previous = last_observed.get(poll.job_id)
            if previous is not None and observed < previous:
                raise ValueError("job poll observations must be chronological per job")
            if last_state.get(poll.job_id) == "succeeded":
                raise ValueError("no job poll may follow terminal success")
            counts[poll.job_id] += 1
            last_observed[poll.job_id] = observed
            last_state[poll.job_id] = poll.state
        for job_id in job_ids:
            if counts[job_id] == 0 or last_state.get(job_id) != "succeeded":
                raise ValueError("every job requires a final poll that observes terminal success")


class SourceBatchManifestReferenceV1(_Frozen):
    """A pointer to sealed manifest bytes, plus what those bytes must be.

    Possessing this reference is not authorization to read the bytes; the
    reader still enforces scope.  It is only enough to *verify* them.
    """

    staged_commit_ref: OpaqueReference
    manifest_sha256: Sha256
    byte_count: int = Field(ge=1, le=SOURCE_BATCH_MANIFEST_MAX_BYTES_V1)
    encoding: Literal["canonical_json_utf8_v1"] = "canonical_json_utf8_v1"


class SourceBatchManifestV1(_Frozen):
    """One immutable publication: what was fetched, from when, and how completely.

    The invariants enforced here are *single-publication* admission rules.
    Cross-revision rules (a snapshot may not regress freshness, a correction
    must follow an authoritative publication) live in
    :func:`adcp.reporting.conformance.validate_reporting_revision_sequence`
    because they need more than one manifest to evaluate.

    Time evidence is ordered: ``data_through`` cannot follow the observation,
    acquisition cannot precede the observation, and finality evidence must be
    observed between ``data_through`` and acquisition.  Authoritative finality
    evidence additionally cannot predate the period end -- you cannot declare a
    period official before it has finished.
    """

    manifest_version: Literal["1.0"] = REPORTING_SOURCE_CONTRACT_VERSION_V1
    strictness: ReportingManifestStrictness = "basic"
    complete: Literal[True] = True
    identity: ReportingSourceIdentityV1
    publication_namespace: Annotated[str, StringConstraints(pattern=_NAMESPACE_PATTERN)]
    publication_id: OpaqueReference
    publication_class: ReportingPublicationClass
    content_fingerprint: Fingerprint
    adapter_build: ReportingAdapterBuildIdentityV1
    offering_id: Identifier
    contract: ReportingContractIdentityV1
    period: ReportingSourcePeriodV1
    revision_kind: ReportingRevisionKind
    supersedes_publication_id: OpaqueReference | None = None
    finality_evidence: ReportingFinalityEvidenceV1
    observed_at: datetime
    data_through: datetime
    acquired_at: datetime
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    requested_dimensions: list[MetricName] = Field(default_factory=list, max_length=1_000)
    objects: list[SourceBatchObjectV1] = Field(min_length=1, max_length=100_000)
    object_set_sha256: Sha256
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    control_totals: list[SourceControlTotalV1] = Field(default_factory=list, max_length=1_000)
    metric_availability: list[ReportingMetricAvailabilityV1] = Field(
        min_length=1, max_length=SOURCE_BATCH_MANIFEST_MAX_METRIC_AVAILABILITY_V1
    )
    coverage: SourceBatchCoverageV1
    explicit_zero: bool = False
    event_time_range: tuple[datetime, datetime] | None = None
    evidence: SourceBatchEvidenceV1 | None = None
    prior_checkpoint: ReportingSourceCheckpointV1 | None = None
    warnings: list[Annotated[str, StringConstraints(min_length=1, max_length=1_024)]] = Field(
        default_factory=list, max_length=1_000
    )

    @model_validator(mode="after")
    def _manifest_is_internally_consistent(self) -> SourceBatchManifestV1:
        self._validate_objects()
        self._validate_time_evidence()
        self._validate_finality()
        self._validate_cells()
        self._validate_zero_rows()
        self._validate_event_range()
        self._validate_strictness()
        expected = publication_content_fingerprint_v1(self)
        if expected != self.content_fingerprint:
            raise ValueError(
                f"content_fingerprint must bind the publication content; expected {expected}"
            )
        return self

    # -- object set ------------------------------------------------------

    def _validate_objects(self) -> None:
        seen_refs: set[str] = set()
        seen_generations: set[tuple[str, str]] = set()
        byte_count = 0
        row_count = 0
        for index, item in enumerate(self.objects):
            if item.ordinal != index:
                raise ValueError("object ordinals must be contiguous and ordered from zero")
            if item.object_ref in seen_refs:
                raise ValueError(f"object_ref {item.object_ref!r} is not unique")
            if (item.object_ref, item.object_generation) in seen_generations:
                raise ValueError("object reference and generation pairs must be unique")
            seen_refs.add(item.object_ref)
            seen_generations.add((item.object_ref, item.object_generation))
            byte_count += item.byte_count
            row_count += item.row_count
        if byte_count != self.byte_count:
            raise ValueError(f"byte_count must equal the object sum, expected {byte_count}")
        if row_count != self.row_count:
            raise ValueError(f"row_count must equal the object sum, expected {row_count}")
        expected = source_batch_object_set_sha256_v1(self.objects)
        if expected != self.object_set_sha256:
            raise ValueError(
                f"object_set_sha256 must bind the ordered pinned objects, expected {expected}"
            )

    # -- time ------------------------------------------------------------

    def _validate_time_evidence(self) -> None:
        start = _aware(self.period.start, "period.start")
        end = _aware(self.period.end, "period.end")
        cutoff = _aware(self.period.source_read_cutoff_at, "period.source_read_cutoff_at")
        observed_at = _aware(self.observed_at, "observed_at")
        data_through = _aware(self.data_through, "data_through")
        acquired_at = _aware(self.acquired_at, "acquired_at")
        finality_at = _aware(self.finality_evidence.observed_at, "finality_evidence.observed_at")
        if not (start <= data_through <= end):
            raise ValueError("data_through must be inside the reporting period")
        if data_through > observed_at or data_through > cutoff:
            raise ValueError("data_through must not follow observed_at or the source read cutoff")
        if acquired_at < observed_at:
            raise ValueError("acquired_at must not precede the reported source observation")
        if finality_at < data_through or finality_at > acquired_at:
            raise ValueError(
                "finality evidence must be observed no earlier than data_through and "
                "no later than acquisition completion"
            )
        if self.publication_class == "AUTHORITATIVE" and finality_at < end:
            raise ValueError("authoritative finality evidence must not predate the period end")
        for item in self.coverage.constituents:
            if item.data_through is not None:
                self._require_bounded_watermark(item.data_through, "constituent")
        for cell in self.metric_availability:
            if cell.data_through is not None:
                self._require_bounded_watermark(cell.data_through, "metric")
        self._validate_authoritative_full_window(end)

    def _require_bounded_watermark(self, value: datetime, label: str) -> None:
        moment = _aware(value, f"{label} data_through")
        if not (
            _aware(self.period.start, "period.start")
            <= moment
            <= min(
                _aware(self.period.end, "period.end"),
                _aware(self.data_through, "data_through"),
                _aware(self.period.source_read_cutoff_at, "period.source_read_cutoff_at"),
            )
        ):
            raise ValueError(
                f"granular {label} data_through must be inside the period and no later than "
                "the batch watermark or the requested source cutoff"
            )

    def _validate_authoritative_full_window(self, end: datetime) -> None:
        # Full official coverage is the strongest claim in the contract: it
        # says the period is closed and complete. Data that never reached the
        # period boundary stays delayed/stale/partial instead.
        if self.publication_class != "AUTHORITATIVE" or self.coverage.status != "full":
            return
        if _aware(self.data_through, "data_through") != end:
            raise ValueError("full authoritative coverage must prove the complete reporting window")
        for item in self.coverage.constituents:
            if item.status in _AVAILABLE_STATUSES and (
                item.data_through is None or _aware(item.data_through, "data_through") != end
            ):
                raise ValueError(
                    "available constituents in full authoritative coverage must prove the "
                    "complete reporting window"
                )
        for cell in self.metric_availability:
            if cell.status in _AVAILABLE_STATUSES and (
                cell.data_through is None or _aware(cell.data_through, "data_through") != end
            ):
                raise ValueError(
                    "available metrics in full authoritative coverage must prove the "
                    "complete reporting window"
                )

    # -- finality --------------------------------------------------------

    def _validate_finality(self) -> None:
        snapshot = self.publication_class == "PROVISIONAL_SNAPSHOT"
        if snapshot != (self.revision_kind == "snapshot"):
            raise ValueError("snapshot and authoritative publications use distinct revision kinds")
        if (self.revision_kind == "correction") != (self.supersedes_publication_id is not None):
            raise ValueError(
                "supersedes_publication_id is required for a correction and forbidden otherwise"
            )
        basis = self.finality_evidence.basis
        if snapshot and basis != "provisional_observation":
            raise ValueError("a snapshot must be identified as a provisional observation")
        if not snapshot and basis == "provisional_observation":
            raise ValueError("authoritative data needs finality evidence beyond an observation")

    # -- cells -----------------------------------------------------------

    def _validate_cells(self) -> None:
        cells = [(cell.constituent_id, cell.metric) for cell in self.metric_availability]
        if len(set(cells)) != len(cells):
            raise ValueError(
                "metric availability must carry one record per constituent-metric cell"
            )
        by_constituent: dict[str, set[str]] = {}
        for cell in self.metric_availability:
            by_constituent.setdefault(cell.constituent_id, set()).add(cell.metric)
        published_metrics = {metric for _, metric in cells}
        statuses = {item.constituent_id: item.status for item in self.coverage.constituents}
        for constituent_id in statuses:
            if by_constituent.get(constituent_id, set()) != published_metrics:
                raise ValueError(
                    f"constituent {constituent_id!r} needs one availability record for every "
                    "published metric"
                )
        for cell in self.metric_availability:
            constituent_status = statuses.get(cell.constituent_id)
            if constituent_status is None:
                raise ValueError(
                    f"metric evidence for {cell.constituent_id!r} is outside the coverage "
                    "denominator"
                )
            if cell.status not in _CONSTITUENT_ALLOWS_METRIC[constituent_status]:
                raise ValueError(
                    f"metric status {cell.status!r} contradicts constituent status "
                    f"{constituent_status!r}"
                )

    # -- zero rows -------------------------------------------------------

    def _validate_zero_rows(self) -> None:
        """A zero-row period has exactly two honest shapes, and they differ.

        ``explicit_zero``: the source answered for every requested cell and the
        answer was zero.  That is data, and it satisfies an obligation.

        Unavailable: the source did not answer.  That is not data, and it must
        not be dressed up as a zero -- otherwise a dead feed looks like a quiet
        campaign forever.
        """
        if self.explicit_zero:
            if self.event_time_range is not None:
                raise ValueError("an explicit-zero batch must not declare an event-time range")
            if self.row_count != 0 or any(item.row_count for item in self.objects):
                raise ValueError("every explicit-zero object must have zero rows")
            if any(not total.is_zero for total in self.control_totals):
                raise ValueError("every explicit-zero control total must be zero")
            if any(item.status != "explicit_zero" for item in self.coverage.constituents) or any(
                cell.status != "explicit_zero" for cell in self.metric_availability
            ):
                raise ValueError(
                    "a batch-wide explicit zero requires explicit-zero evidence for every "
                    "requested constituent and metric"
                )
            return
        if self.row_count == 0:
            unavailable = (
                self.coverage.status != "full"
                and all(
                    item.status not in _AVAILABLE_STATUSES for item in self.coverage.constituents
                )
                and all(cell.status not in _AVAILABLE_STATUSES for cell in self.metric_availability)
            )
            if not unavailable:
                raise ValueError("a completed zero-row available batch must be explicitly zero")
            if self.event_time_range is not None:
                raise ValueError(
                    "a zero-row unavailable batch must not declare an event-time range"
                )
            if any(not total.is_zero for total in self.control_totals):
                raise ValueError("a zero-row unavailable batch cannot declare nonzero totals")
            return
        if self.event_time_range is None:
            raise ValueError("a nonzero batch must declare its event-time range")

    def _validate_event_range(self) -> None:
        if self.event_time_range is None:
            return
        # Row-level evidence, not a second freshness clock: rows beyond the
        # watermark or the frozen cutoff make the manifest self-contradictory.
        event_start = _aware(self.event_time_range[0], "event_time_range start")
        event_end = _aware(self.event_time_range[1], "event_time_range end")
        if event_end < event_start:
            raise ValueError("event-time range end must not precede its start")
        if event_start < _aware(self.period.start, "period.start"):
            raise ValueError("event-time range must start within the period")
        if event_end > _aware(self.period.end, "period.end"):
            raise ValueError("event-time range must end within the period")
        if event_end > _aware(self.data_through, "data_through") or event_end > _aware(
            self.period.source_read_cutoff_at, "period.source_read_cutoff_at"
        ):
            raise ValueError(
                "event-time range must end no later than data_through or the source cutoff"
            )

    def _validate_strictness(self) -> None:
        if self.strictness == "evidenced":
            if self.evidence is None:
                raise ValueError("an evidenced manifest must retain per-call provider evidence")
            evidenced_rows = sum(page.item_count for page in self.evidence.pages)
            if evidenced_rows != self.row_count:
                raise ValueError(
                    "evidenced page item counts must equal the normalized row count, "
                    f"expected {self.row_count}"
                )
            ordinals = [
                ordinal for page in self.evidence.pages for ordinal in page.output_object_ordinals
            ]
            if sorted(ordinals) != [item.ordinal for item in self.objects]:
                raise ValueError("page evidence must exactly partition the staged object set")
            acquired_at = _aware(self.acquired_at, "acquired_at")
            for poll in self.evidence.polls:
                if _aware(poll.observed_at, "observed_at") > acquired_at:
                    raise ValueError("job poll observations must not postdate acquisition")
            for attempt in self.evidence.retry_attempts:
                if _aware(attempt.attempted_at, "attempted_at") > acquired_at:
                    raise ValueError("retry attempts must not postdate acquisition")
        elif self.evidence is not None:
            raise ValueError(
                "a basic manifest must omit per-call provider evidence; "
                "declare strictness='evidenced' to retain it"
            )


# --------------------------------------------------------------------------
# Fingerprints and sealing
# --------------------------------------------------------------------------


def source_batch_object_set_sha256_v1(objects: Sequence[SourceBatchObjectV1]) -> str:
    """Length-prefixed digest over the ordered, generation-pinned object set.

    Length prefixing rather than concatenation: without it, two different
    object sets whose fields happen to concatenate to the same string would
    collide.
    """
    import hashlib

    digest = hashlib.sha256()

    def append(value: str) -> None:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)

    append("ordered_generation_pinned_object_set_v1")
    for item in objects:
        append(str(item.ordinal))
        append(item.object_ref)
        append(item.object_generation)
        append(item.media_type)
        append(item.compression)
        append(item.sha256)
        append(str(item.byte_count))
        append(str(item.row_count))
    return digest.hexdigest()


def source_batch_coverage_fingerprint_v1(
    constituents: Sequence[ReportingConstituentCoverageV1],
) -> str:
    """Fingerprint the complete constituent *publication evidence*, status included."""
    ordered = sorted(
        (item.canonical_payload() for item in constituents),
        key=lambda item: str(item["constituent"]["constituent_id"]).encode(
            "utf-16-be", errors="surrogatepass"
        ),
    )
    return reporting_fingerprint_v1(
        {"kind": "reporting_coverage_evidence_v1", "constituents": ordered}
    )


def publication_content_fingerprint_v1(manifest: SourceBatchManifestV1 | Mapping[str, Any]) -> str:
    """Fingerprint the source-neutral *content* of a publication.

    Deliberately excludes acquisition timing, staged object references, and
    run identity: two fetches of an unchanged quiet campaign produce equal
    content fingerprints, which lets a producer record the successful refresh
    without committing a duplicate revision. A later changed observation still
    receives its own immutable publication and supersedes the prior snapshot.
    """
    raw = (
        manifest.model_dump(mode="json", exclude_none=True)
        if isinstance(manifest, BaseModel)
        else dict(manifest)
    )
    payload: dict[str, Any] = dict(strip_absent(raw))
    content = {
        "publication_class": payload["publication_class"],
        "publication_namespace": payload["publication_namespace"],
        "offering_id": payload["offering_id"],
        "contract": payload["contract"],
        "currency": payload["currency"],
        "requested_dimensions": payload.get("requested_dimensions", []),
        "period": {
            key: payload["period"][key]
            for key in ("period_key", "start", "end", "source_timezone", "grain", "windowing")
        },
        "account_id": payload["identity"]["account_id"],
        "report_definition_id": payload["identity"]["report_definition_id"],
        "source_scope": payload["identity"].get("source_scope", {}),
        "objects": [
            {
                key: item[key]
                for key in (
                    "ordinal",
                    "media_type",
                    "compression",
                    "sha256",
                    "byte_count",
                    "row_count",
                )
            }
            for item in payload["objects"]
        ],
        "row_count": payload["row_count"],
        "control_totals": payload.get("control_totals", []),
        "metric_availability": [
            {key: value for key, value in cell.items() if key != "data_through"}
            for cell in payload["metric_availability"]
        ],
        "coverage": {
            "denominator_fingerprint": payload["coverage"]["denominator_fingerprint"],
            "status": payload["coverage"]["status"],
            "constituents": [
                {key: value for key, value in item.items() if key != "data_through"}
                for item in payload["coverage"]["constituents"]
            ],
        },
        "explicit_zero": payload.get("explicit_zero", False),
    }
    return reporting_fingerprint_v1(content)


def deterministic_source_publication_id_v1(
    *,
    source_execution_key: str,
    logical_slice_fingerprint: str,
    publication_class: str,
    revision_kind: str,
    supersedes_publication_id: str | None = None,
) -> str:
    """The publication id a conforming executor MUST mint for this slice.

    Derived, not chosen, so replaying a ``source_execution_key`` cannot produce
    a second publication id for the same logical work.
    """
    identity: dict[str, Any] = {
        "source_execution_key": source_execution_key,
        "logical_slice_fingerprint": logical_slice_fingerprint,
        "publication_class": publication_class,
        "revision_kind": revision_kind,
    }
    if supersedes_publication_id:
        identity["supersedes_publication_id"] = supersedes_publication_id
    return f"src-{canonical_json_sha256_v1(identity)}"


class SourceBatchManifestError(RuntimeError):
    """A manifest's bytes are unusable: too large, mis-hashed, or non-canonical."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def encode_source_batch_manifest_v1(manifest: SourceBatchManifestV1) -> bytes:
    """Seal a manifest into its ``canonical_json_utf8_v1`` bytes."""
    payload = manifest.model_dump(mode="json", exclude_none=True)
    encoded = canonical_json_utf8_v1(payload)
    if len(encoded) > SOURCE_BATCH_MANIFEST_MAX_BYTES_V1:
        raise SourceBatchManifestError(
            "SOURCE_MANIFEST_TOO_LARGE",
            "the source manifest exceeds the 1 MiB retained-evidence limit; "
            "large data belongs in staged objects",
        )
    return encoded


def source_batch_manifest_reference_v1(
    staged_commit_ref: str, manifest_bytes: bytes
) -> SourceBatchManifestReferenceV1:
    """Bind sealed manifest bytes to a reference a consumer can verify."""
    import hashlib

    if len(manifest_bytes) > SOURCE_BATCH_MANIFEST_MAX_BYTES_V1:
        raise SourceBatchManifestError(
            "SOURCE_MANIFEST_TOO_LARGE",
            "the source manifest exceeds the 1 MiB retained-evidence limit",
        )
    return SourceBatchManifestReferenceV1(
        staged_commit_ref=staged_commit_ref,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        byte_count=len(manifest_bytes),
    )


def parse_verified_source_batch_manifest_v1(
    reference: SourceBatchManifestReferenceV1, raw_bytes: bytes
) -> SourceBatchManifestV1:
    """Verify size, exact bytes, UTF-8, JSON, schema, *and* canonical encoding.

    The last check is the one adopters forget.  Bytes that parse into a valid
    manifest but do not re-encode to themselves are not
    ``canonical_json_utf8_v1``; accepting them means two consumers can hold the
    same logical manifest under two different digests.
    """
    import hashlib
    import json

    if (
        len(raw_bytes) > SOURCE_BATCH_MANIFEST_MAX_BYTES_V1
        or len(raw_bytes) != reference.byte_count
    ):
        raise SourceBatchManifestError(
            "SOURCE_MANIFEST_TOO_LARGE",
            "the staged manifest size does not match the bounded reference",
        )
    if hashlib.sha256(raw_bytes).hexdigest() != reference.manifest_sha256:
        raise SourceBatchManifestError(
            "SOURCE_MANIFEST_CHECKSUM_MISMATCH",
            "the staged manifest bytes do not match the declared checksum",
        )
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceBatchManifestError(
            "SOURCE_MANIFEST_ENCODING_INVALID", "the staged manifest is not valid UTF-8"
        ) from error
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise SourceBatchManifestError(
            "SOURCE_MANIFEST_JSON_INVALID", "the staged manifest is not valid JSON"
        ) from error
    manifest = SourceBatchManifestV1.model_validate(payload)
    if encode_source_batch_manifest_v1(manifest) != raw_bytes:
        raise SourceBatchManifestError(
            "SOURCE_MANIFEST_CANONICAL_ENCODING_MISMATCH",
            "the staged manifest is not canonical_json_utf8_v1",
        )
    return manifest


# --------------------------------------------------------------------------
# Execution result
# --------------------------------------------------------------------------


class ReportingSourceExecutionResponseV1(_Frozen):
    """The success arm: the frozen identity echoed back, plus a manifest reference."""

    contract_version: Literal["1.0"] = REPORTING_SOURCE_CONTRACT_VERSION_V1
    identity: ReportingSourceIdentityV1
    outcome: Literal["completed"] = "completed"
    manifest: SourceBatchManifestReferenceV1


class ReportingSourceErrorV1(_Frozen):
    """The failure arm: a stable code, a retry classification, and a safe message.

    ``safe_message`` is retained and may be shown to a counterparty.  It must
    not carry tokens, signed URLs, raw upstream bodies, or customer secrets --
    put those in redacted observability instead.
    """

    contract_version: Literal["1.0"] = REPORTING_SOURCE_CONTRACT_VERSION_V1
    code: ReportingSourceErrorCode
    retry: ReportingSourceRetryClass
    scope: Literal["slice", "account", "source"] = "slice"
    safe_message: Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
    provider_code: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _classification_is_permitted(self) -> ReportingSourceErrorV1:
        if self.retry not in _PERMITTED_RETRY_CLASSES[self.code]:
            raise ValueError(f"error {self.code} cannot be classified {self.retry}")
        if self.retry_after_seconds is not None and self.code not in _BACKOFF_EVIDENCE_CODES:
            raise ValueError("retry_after_seconds is reserved for upstream backoff evidence")
        return self


class ReportingSourceError(Exception):
    """Raise this from an executor to settle the slice as a typed failure.

    Convenience for the common case where the failure is discovered deep in a
    call stack.  :mod:`adcp.reporting.inline_source` and
    :class:`~adcp.reporting.ledger.ReportingProducer` both convert it into the
    corresponding :class:`ReportingSourceErrorV1`.
    """

    def __init__(
        self,
        code: ReportingSourceErrorCode,
        safe_message: str,
        *,
        retry: ReportingSourceRetryClass | None = None,
        scope: Literal["slice", "account", "source"] = "slice",
        provider_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(safe_message)
        permitted = _PERMITTED_RETRY_CLASSES[code]
        self.error = ReportingSourceErrorV1(
            code=code,
            retry=retry if retry is not None else _default_retry_class(code),
            scope=scope,
            safe_message=safe_message,
            provider_code=provider_code,
            retry_after_seconds=retry_after_seconds,
        )
        if self.error.retry not in permitted:  # pragma: no cover - guarded by the model
            raise ValueError(f"error {code} cannot be classified {self.error.retry}")


def _default_retry_class(code: ReportingSourceErrorCode) -> ReportingSourceRetryClass:
    """Pick the *safest* permitted classification when the caller did not.

    "Retryable" is the safe default only where the code implies a transient
    upstream condition; everywhere else the safe default is "terminal", because
    retrying a bad credential or a malformed request forever is worse than
    surfacing it.
    """
    permitted = _PERMITTED_RETRY_CLASSES[code]
    candidates: tuple[ReportingSourceRetryClass, ...] = ("cancelled", "retryable", "terminal")
    for candidate in candidates:
        if candidate in permitted and (candidate != "retryable" or code in _BACKOFF_EVIDENCE_CODES):
            return candidate
    return "terminal"


class ReportingSourceExecutorResult(_Frozen):
    """Exactly one of: a completed publication, or a typed failure."""

    response: ReportingSourceExecutionResponseV1 | None = None
    manifest_bytes: bytes | None = None
    error: ReportingSourceErrorV1 | None = None

    @model_validator(mode="after")
    def _exactly_one_arm(self) -> ReportingSourceExecutorResult:
        completed = self.response is not None
        if completed == (self.error is not None):
            raise ValueError("an executor result must be exactly one of completed or failed")
        if completed and self.manifest_bytes is None:
            raise ValueError("a completed result must carry its sealed manifest bytes")
        if not completed and self.manifest_bytes is not None:
            raise ValueError("a failed result must not carry manifest bytes")
        return self

    @property
    def ok(self) -> bool:
        return self.response is not None

    @classmethod
    def completed(
        cls,
        *,
        request: ReportingSourceSliceRequestV1,
        manifest: SourceBatchManifestReferenceV1,
        manifest_bytes: bytes,
    ) -> ReportingSourceExecutorResult:
        return cls(
            response=completed_reporting_source_response_v1(request=request, manifest=manifest),
            manifest_bytes=manifest_bytes,
        )

    @classmethod
    def failed(cls, error: ReportingSourceErrorV1) -> ReportingSourceExecutorResult:
        return cls(error=error)


def completed_reporting_source_response_v1(
    *, request: ReportingSourceSliceRequestV1, manifest: SourceBatchManifestReferenceV1
) -> ReportingSourceExecutionResponseV1:
    """Build the success arm, echoing the frozen request identity verbatim."""
    return ReportingSourceExecutionResponseV1(identity=request.identity, manifest=manifest)


# --------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------


@runtime_checkable
class ReportingSourceExecutor(Protocol):
    """Fetches exactly one frozen slice.

    Cancellation is cooperative and *binding*: when ``cancel`` is set, an
    executor must stop its upstream requests, its job polling, and its staging
    I/O, and only then return.  Returning early while leaving work running in
    the background is not conforming -- the producer's next attempt would race
    the abandoned one for the same staged object names.

    ``heartbeat`` is optional and advisory.  Call it during long polls so the
    producer can distinguish "still working" from "hung" and avoid re-leasing
    the job out from under you.
    """

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        """This build's truthful, scoped capability declaration."""
        ...

    async def execute(
        self,
        request: ReportingSourceSliceRequestV1,
        *,
        cancel: asyncio.Event,
        heartbeat: Callable[[], None] | None = None,
    ) -> ReportingSourceExecutorResult: ...


@runtime_checkable
class ReportingSourceStagedObjectReader(Protocol):
    """Reads one immutable, generation-pinned staged object.

    The reader MUST authorize the ``account_id`` / ``source_scope`` pair before
    returning bytes.  Possession of an opaque object reference is not
    authorization: references leak into logs, manifests, and support tickets.
    """

    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Mapping[str, Any],
        cancel: asyncio.Event,
    ) -> bytes: ...
