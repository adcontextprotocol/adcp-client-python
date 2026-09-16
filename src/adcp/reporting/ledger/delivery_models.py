"""Retained seller delivery/reconciliation facts. No provider clients or wire blobs.

Every collection is a tuple, every nested value is frozen, and there is no open
metadata bag. The trusted binding reference selects configuration in a separate
credential resolver; it is never itself a credential or an authorization grant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import datetime
from types import UnionType
from typing import Any, ClassVar, Literal, TypeAlias, Union, get_args, get_origin, get_type_hints

from pydantic import ConfigDict

from adcp.reporting.currency import validate_currency
from adcp.reporting.evidence import (
    ReportingCanonicalDigest,
    aware_utc,
    consumer_commit_reference,
    destination_reference,
    file_object_reference,
    native_version_reference,
    principal_reference,
    reader_feature_reference,
    reporting_identifier,
    resource_location,
    sha256_value,
)
from adcp.reporting.evidence import ReportingControlTotalRecord as ReportingControlTotalRecord
from adcp.reporting.ledger.models import ReportingConfigurationGenerationKey

DeliveryMethod = Literal["file_transfer", "dataset_share", "warehouse_materialization"]
ReportingFormat = Literal["jsonl", "csv", "parquet", "avro", "orc"]
VerificationProfile = Literal["canonical_digest", "manifest_checksums", "native_commit"]
VerificationPath = Literal["producer", "representative_consumer", "destination"]
ReceiptStatus = Literal["accepted", "rejected"]
ReportingReconciliationRecordKind = Literal[
    "destination_binding",
    "obligation_delivery",
    "materialization_attempt",
    "materialization",
    "materialization_check",
    "revision_receipt",
    "adjustment_receipt",
]
MaterializationFailure = Literal[
    "WRITE_FAILED", "VERIFICATION_FAILED", "CONTENT_CORRUPT", "RESOURCE_UNAVAILABLE"
]


class _ClosedValue:
    __slots__ = ()
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", hide_input_in_errors=True
    )


def _hints(cls: type[Any]) -> dict[str, Any]:
    return get_type_hints(cls)


def _freeze(value: Any, annotation: Any) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (UnionType, Union):
        for member in args:
            try:
                return _freeze(value, member)
            except ValueError:
                continue
    elif origin is Literal:
        if value in args and type(value) is type(args[0]):
            return value
    elif origin is tuple and isinstance(value, (tuple, list)):
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_freeze(item, args[0]) for item in value)
        if len(args) == len(value):
            return tuple(_freeze(item, expected) for item, expected in zip(value, args))
    elif type(value) is annotation:
        return value
    raise ValueError("reporting records require closed immutable typed values")


def _freeze_fields(record: Any) -> None:
    hints = _hints(type(record))
    for item in fields(record):
        object.__setattr__(record, item.name, _freeze(getattr(record, item.name), hints[item.name]))


def _positive(value: int, *, zero: bool = False) -> None:
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError("reporting evidence has an invalid count")


def _codes(values: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(values)
    if len(set(values)) != len(values) or any(
        not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", value) is None
        for value in values
    ):
        raise ValueError("reporting rejection codes require unique safe classifications")
    return values


@dataclass(frozen=True, slots=True)
class ReportingDeliveryPrincipal(_ClosedValue):
    """Trusted account and consumer identity, resolved from authenticated transport."""

    account_id: str
    consumer_id: str

    def __post_init__(self) -> None:
        _freeze_fields(self)
        principal_reference(self.account_id)
        from adcp.reporting.evidence import consumer_reference

        consumer_reference(self.consumer_id)


@dataclass(frozen=True, slots=True)
class ReportingDeliveryScope(_ClosedValue):
    generation_key: ReportingConfigurationGenerationKey
    consumer_id: str
    reporting_obligation_id: str

    def __post_init__(self) -> None:
        _freeze_fields(self)
        if type(self.generation_key) is not ReportingConfigurationGenerationKey:
            raise ValueError("reporting scope requires the typed configuration generation")
        self.principal
        reporting_identifier(self.generation_key.delivery_config_id, maximum=64)
        _positive(self.generation_key.delivery_config_version)
        reporting_identifier(self.reporting_obligation_id, maximum=255)

    @property
    def principal(self) -> ReportingDeliveryPrincipal:
        return ReportingDeliveryPrincipal(self.generation_key.account_id, self.consumer_id)


@dataclass(frozen=True, slots=True)
class ReportingMaterializationKey(_ClosedValue):
    principal: ReportingDeliveryPrincipal
    reporting_materialization_id: str

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_materialization_id, maximum=255)


@dataclass(frozen=True, slots=True)
class ReportingReceiptKey(_ClosedValue):
    principal: ReportingDeliveryPrincipal
    reporting_receipt_id: str

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_receipt_id, maximum=255)
        if len(self.reporting_receipt_id) < 16:
            raise ValueError("a reporting receipt identifier requires at least 16 characters")


@dataclass(frozen=True, slots=True)
class ReportingDestinationBinding(_ClosedValue):
    """Immutable public portion of one trusted principal/configuration binding.

    ``trusted_binding_ref`` identifies an immutable trusted configuration, not a
    mutable 'latest' alias. Credentials are resolved later behind that reference.
    Storing this record neither configures a writer nor advertises a capability.
    """

    generation_key: ReportingConfigurationGenerationKey
    consumer_id: str
    destination_ref: str
    trusted_binding_ref: str = field(repr=False)
    method: DeliveryMethod
    transport: str
    verification_profile: VerificationProfile
    reconciliation_mode: Literal["delivery_only", "consumer_receipt"]
    feed_purpose: Literal["pacing", "analytics", "billing"]
    resource_retention_days: int
    created_at: datetime
    format: ReportingFormat | None = None
    reader_compatibility: tuple[str, ...] = ()
    success_status: Literal["available", "delivered"] = "available"
    kind: Literal["destination_binding"] = field(default="destination_binding", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        ReportingDeliveryScope(self.generation_key, self.consumer_id, "binding")
        destination_reference(self.destination_ref)
        destination_reference(self.trusted_binding_ref)
        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.transport) is None:
            raise ValueError("reporting transport requires a public protocol label")
        reporting_identifier(self.transport, maximum=64)
        _positive(self.resource_retention_days)
        object.__setattr__(self, "created_at", aware_utc(self.created_at))
        object.__setattr__(
            self,
            "reader_compatibility",
            tuple(reader_feature_reference(value) for value in self.reader_compatibility),
        )
        if len(set(self.reader_compatibility)) != len(self.reader_compatibility):
            raise ValueError("reader compatibility requirements must be unique")
        if self.method == "file_transfer" and self.format is None:
            raise ValueError("file transfer requires a declared format")
        if self.verification_profile == "manifest_checksums" and self.method != "file_transfer":
            raise ValueError("manifest checksum verification requires file transfer")
        if self.method == "warehouse_materialization" and self.success_status != "delivered":
            raise ValueError("warehouse materialization requires destination delivery")
        if self.method == "dataset_share" and self.success_status != "available":
            raise ValueError("dataset share requires representative-consumer availability")
        if self.feed_purpose == "billing" and self.verification_profile != "canonical_digest":
            raise ValueError("billing receipts require canonical digest verification")

    @property
    def principal(self) -> ReportingDeliveryPrincipal:
        return ReportingDeliveryPrincipal(self.generation_key.account_id, self.consumer_id)


@dataclass(frozen=True, slots=True)
class ReportingObligationDeliveryRecord(_ClosedValue):
    scope: ReportingDeliveryScope
    currency: str
    resource_retained_until: datetime
    created_at: datetime
    kind: Literal["obligation_delivery"] = field(default="obligation_delivery", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        validate_currency(self.currency)
        object.__setattr__(self, "resource_retained_until", aware_utc(self.resource_retained_until))
        object.__setattr__(self, "created_at", aware_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class ReportingMaterializationAttempt(_ClosedValue):
    scope: ReportingDeliveryScope
    reporting_revision_id: str
    reporting_materialization_id: str
    attempt: int
    created_at: datetime
    kind: Literal["materialization_attempt"] = field(
        default="materialization_attempt", kw_only=True
    )

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_revision_id, maximum=255)
        reporting_identifier(self.reporting_materialization_id, maximum=255)
        _positive(self.attempt)
        object.__setattr__(self, "created_at", aware_utc(self.created_at))

    @property
    def key(self) -> ReportingMaterializationKey:
        return ReportingMaterializationKey(self.scope.principal, self.reporting_materialization_id)


@dataclass(frozen=True, slots=True)
class ReportingResourceRecord(_ClosedValue):
    resource_ref: str
    kind: Literal["manifest", "dataset", "warehouse_relation"]
    location: str
    immutability: Literal["immutable_location", "native_version"]
    expires_at: datetime
    native_version_ref: str | None = None
    manifest_sha256: str | None = None
    object_refs: tuple[str, ...] = ()
    reader_compatibility: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.resource_ref, maximum=255)
        resource_location(self.location)
        if self.native_version_ref is not None:
            native_version_reference(self.native_version_ref)
        if self.manifest_sha256 is not None:
            sha256_value(self.manifest_sha256)
        if self.kind == "manifest" and self.manifest_sha256 is None:
            raise ValueError("manifest resources require the exact manifest digest")
        if self.immutability == "native_version" and self.native_version_ref is None:
            raise ValueError("native-version resources require an immutable version")
        object.__setattr__(self, "expires_at", aware_utc(self.expires_at))
        object.__setattr__(
            self,
            "object_refs",
            tuple(file_object_reference(value) for value in self.object_refs),
        )
        object.__setattr__(
            self,
            "reader_compatibility",
            tuple(reader_feature_reference(value) for value in self.reader_compatibility),
        )
        if len(set(self.object_refs)) != len(self.object_refs):
            raise ValueError("resource object references must be unique")
        if len(set(self.reader_compatibility)) != len(self.reader_compatibility):
            raise ValueError("reader compatibility requirements must be unique")


@dataclass(frozen=True, slots=True)
class ReportingPhysicalChecksum(_ClosedValue):
    object_ref: str
    algorithm: Literal["sha256", "sha512"]
    value: str

    def __post_init__(self) -> None:
        _freeze_fields(self)
        file_object_reference(self.object_ref)
        size = 64 if self.algorithm == "sha256" else 128
        if (
            not isinstance(self.value, str)
            or re.fullmatch(rf"[a-fA-F0-9]{{{size}}}", self.value) is None
        ):
            raise ValueError("physical checksum length must match its algorithm")


def _unique_totals(totals: tuple[ReportingControlTotalRecord, ...]) -> None:
    if len({item.name for item in totals}) != len(totals):
        raise ValueError("reporting control total names must be unique")


@dataclass(frozen=True, slots=True)
class ReportingVerificationRecord(_ClosedValue):
    verified_at: datetime
    verification_path: VerificationPath
    verification_profile: VerificationProfile
    row_count: int
    control_totals: tuple[ReportingControlTotalRecord, ...]
    canonical_content_digest: ReportingCanonicalDigest | None = None
    physical_checksums: tuple[ReportingPhysicalChecksum, ...] = ()
    native_version_ref: str | None = None
    native_observed_through: Literal["representative_consumer", "destination"] | None = None
    verified_format: ReportingFormat | None = None

    def __post_init__(self) -> None:
        _freeze_fields(self)
        object.__setattr__(self, "verified_at", aware_utc(self.verified_at))
        _positive(self.row_count, zero=True)
        _unique_totals(self.control_totals)
        object.__setattr__(self, "physical_checksums", tuple(self.physical_checksums))
        if self.native_version_ref is not None:
            native_version_reference(self.native_version_ref)
        if self.native_observed_through is not None and self.native_version_ref is None:
            raise ValueError("native observation paths require version evidence")


@dataclass(frozen=True, slots=True)
class ReportingMaterializationRecord(_ClosedValue):
    """One terminal outcome. The pending attempt is retained separately."""

    scope: ReportingDeliveryScope
    reporting_revision_id: str
    reporting_materialization_id: str
    status: Literal["available", "delivered", "failed"]
    completed_at: datetime
    resource: ReportingResourceRecord | None = None
    verification: ReportingVerificationRecord | None = None
    failure_code: MaterializationFailure | None = None
    kind: Literal["materialization"] = field(default="materialization", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_revision_id, maximum=255)
        reporting_identifier(self.reporting_materialization_id, maximum=255)
        object.__setattr__(self, "completed_at", aware_utc(self.completed_at))
        if self.status == "failed":
            if (
                self.failure_code is None
                or self.resource is not None
                or self.verification is not None
            ):
                raise ValueError(
                    "failed materializations retain only a safe failure classification"
                )
        elif self.resource is None or self.verification is None or self.failure_code is not None:
            raise ValueError(
                "successful materializations require resource and verification evidence"
            )

    @property
    def key(self) -> ReportingMaterializationKey:
        return ReportingMaterializationKey(self.scope.principal, self.reporting_materialization_id)


@dataclass(frozen=True, slots=True)
class ReportingMaterializationCheck(_ClosedValue):
    """Append-only storage observations; they never mutate terminal evidence."""

    scope: ReportingDeliveryScope
    reporting_materialization_id: str
    check_id: str
    state: Literal["readable", "unavailable", "corrupt"]
    checked_at: datetime
    kind: Literal["materialization_check"] = field(default="materialization_check", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        reporting_identifier(self.reporting_materialization_id, maximum=255)
        reporting_identifier(self.check_id, maximum=255)
        object.__setattr__(self, "checked_at", aware_utc(self.checked_at))


@dataclass(frozen=True, slots=True)
class ReportingRevisionReceiptRecord(_ClosedValue):
    scope: ReportingDeliveryScope
    reporting_receipt_id: str
    reporting_revision_id: str
    reporting_materialization_id: str
    status: ReceiptStatus
    verification_profile: VerificationProfile
    observed_row_count: int
    observed_control_totals: tuple[ReportingControlTotalRecord, ...]
    observed_at: datetime
    supersedes_reporting_receipt_id: str | None = None
    observed_canonical_content_digest: ReportingCanonicalDigest | None = None
    observed_manifest_sha256: str | None = None
    observed_native_version_ref: str | None = None
    consumer_commit_ref: str | None = None
    rejection_codes: tuple[str, ...] = ()
    received_at: datetime | None = None
    kind: Literal["revision_receipt"] = field(default="revision_receipt", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        self.key
        reporting_identifier(self.reporting_revision_id, maximum=255)
        reporting_identifier(self.reporting_materialization_id, maximum=255)
        _positive(self.observed_row_count, zero=True)
        _unique_totals(self.observed_control_totals)
        if self.observed_manifest_sha256 is not None:
            sha256_value(self.observed_manifest_sha256)
        if self.observed_native_version_ref is not None:
            native_version_reference(self.observed_native_version_ref)
        if self.consumer_commit_ref is not None:
            consumer_commit_reference(self.consumer_commit_ref)
        _receipt_fields(self)

    @property
    def key(self) -> ReportingReceiptKey:
        return ReportingReceiptKey(self.scope.principal, self.reporting_receipt_id)


@dataclass(frozen=True, slots=True)
class ReportingAdjustmentReceiptRecord(_ClosedValue):
    scope: ReportingDeliveryScope
    reporting_receipt_id: str
    reporting_adjustment_id: str
    adjusts_reporting_revision_id: str
    status: ReceiptStatus
    observed_adjustment_sha256: str
    observed_at: datetime
    supersedes_reporting_receipt_id: str | None = None
    rejection_codes: tuple[str, ...] = ()
    received_at: datetime | None = None
    kind: Literal["adjustment_receipt"] = field(default="adjustment_receipt", kw_only=True)

    def __post_init__(self) -> None:
        _freeze_fields(self)
        self.key
        reporting_identifier(self.reporting_adjustment_id, maximum=255)
        reporting_identifier(self.adjusts_reporting_revision_id, maximum=255)
        sha256_value(self.observed_adjustment_sha256)
        _receipt_fields(self)

    @property
    def key(self) -> ReportingReceiptKey:
        return ReportingReceiptKey(self.scope.principal, self.reporting_receipt_id)


ReportingReceiptRecord: TypeAlias = (
    ReportingRevisionReceiptRecord | ReportingAdjustmentReceiptRecord
)


def _receipt_fields(record: ReportingReceiptRecord) -> None:
    if record.supersedes_reporting_receipt_id is not None:
        ReportingReceiptKey(record.scope.principal, record.supersedes_reporting_receipt_id)
    object.__setattr__(record, "observed_at", aware_utc(record.observed_at))
    if record.received_at is not None:
        object.__setattr__(record, "received_at", aware_utc(record.received_at))
    object.__setattr__(record, "rejection_codes", _codes(record.rejection_codes))
    if (record.status == "rejected") != bool(record.rejection_codes):
        raise ValueError("only rejected receipts carry rejection codes")


ReportingDeliveryRecord: TypeAlias = (
    ReportingDestinationBinding
    | ReportingObligationDeliveryRecord
    | ReportingMaterializationAttempt
    | ReportingMaterializationRecord
    | ReportingMaterializationCheck
    | ReportingRevisionReceiptRecord
    | ReportingAdjustmentReceiptRecord
)
