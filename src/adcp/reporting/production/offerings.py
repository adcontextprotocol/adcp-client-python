"""Atomic offerings bind public promises to the actual producer and verifier."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from adcp.reporting._timestamp import aware_timestamp
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.delivery_models import ReportingDestinationBinding
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    _schedule_clock,
    iso_duration_to_timedelta,
)
from adcp.reporting.ledger.producer import ReportingProducer
from adcp.reporting.materializer.contracts import ReportingVerificationKey, failure
from adcp.reporting.materializer.verification import _same_definition
from adcp.reporting.production.contracts import (
    ReportingProductionSource,
    ReportingProductionSourceBinding,
)
from adcp.reporting.source import (
    AuthoritativeOfferingV1,
    ProvisionalSnapshotOfferingV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceStagedObjectReader,
    iso_duration_milliseconds_v1,
)
from adcp.types import ReportingDeliveryOffering


@dataclass(frozen=True)
class ReportingProductionOffering:
    """One public offering and the exact running components that implement it.

    The wire model is copied into immutable bytes so mutating an adopter's
    Pydantic model after construction cannot change the advertised contract.
    Capability availability is still checked against live components.
    """

    offering: ReportingDeliveryOffering = field(repr=False, compare=False)
    producer: ReportingProducer = field(repr=False, compare=False)
    verification_key: ReportingVerificationKey
    source_offering_id: str
    _wire: bytes = field(init=False, repr=False)
    _producer_key: str = field(init=False, repr=False)
    _source_identity: int = field(init=False, repr=False)
    _reader_identity: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        checked = ReportingDeliveryOffering.model_validate(
            self.offering.model_dump(mode="json", exclude_none=True)
        )
        raw = checked.model_dump(mode="json", exclude_none=True)
        from adcp.validation.schema_loader import get_named_validator

        validator = get_named_validator("core/reporting-delivery-offering.json")
        if validator is None or next(validator.iter_errors(raw), None) is not None:
            raise ValueError("offering must satisfy the complete public contract")
        profile, definition, key = (
            raw["reporting_profile"],
            self.verification_key.definition,
            self.verification_key,
        )
        method = raw.get("method")
        if (
            method is None
            or method.get("orchestration") != "producer_managed"
            or (method["pattern"], method["transport"], method.get("format"))
            != (key.capability.method, key.capability.transport, key.capability.format)
            or (raw["report_definition_id"], profile["id"])
            != (key.report_definition_id, key.reporting_profile)
            or (raw["report_definition_uri"], raw["report_definition_sha256"].lower())
            != (definition.report_definition_uri, definition.report_definition_sha256)
            or (
                profile["version"],
                profile["schema_uri"],
                profile["schema_sha256"].lower(),
                profile["schema_dialect"],
                profile["schema_ref_policy"],
            )
            != (
                definition.schema_version,
                definition.schema_uri,
                definition.schema_sha256,
                definition.schema_dialect,
                definition.schema_ref_policy,
            )
            or not raw["supported_finality"]
            or len(set(raw["supported_finality"])) != len(raw["supported_finality"])
        ):
            raise ValueError("offering must match its installed producer and exact verifier")
        if raw["reconciliation_mode"] == "consumer_receipt":
            if (
                raw["supported_finality"] != ["official"]
                or key.capability.verification_profile != "canonical_digest"
                or (
                    profile.get("canonicalization_id"),
                    profile.get("canonicalization_uri"),
                    str(profile.get("canonicalization_sha256", "")).lower(),
                )
                != (
                    key.canonicalization.canonicalization_id,
                    key.canonicalization.canonicalization_uri,
                    key.canonicalization.canonicalization_sha256,
                )
            ):
                raise ValueError("reconciled offerings require official canonical receipt evidence")
        elif raw["feed_purpose"] == "billing":
            raise ValueError("billing offerings require consumer receipts")
        object.__setattr__(self, "_wire", canonical_json_utf8_v1(raw))
        object.__setattr__(self, "_source_identity", id(self.producer._source))
        object.__setattr__(self, "_reader_identity", id(self.producer._object_reader))
        object.__setattr__(
            self,
            "_producer_key",
            hashlib.sha256(
                canonical_json_utf8_v1(
                    {
                        "offering": raw,
                        "source_offering_id": self.source_offering_id,
                        "publication_namespace": self.producer._offerings.publication_namespace,
                        "source_scope": dict(self.producer._offerings.source_scope),
                    }
                )
            ).hexdigest(),
        )
        self.check_source()

    @property
    def offering_id(self) -> str:
        return str(self.wire()["offering_id"])

    @property
    def reconciled(self) -> bool:
        return bool(self.wire()["reconciliation_mode"] == "consumer_receipt")

    def wire(self) -> dict[str, Any]:
        return dict(json.loads(self._wire))

    def check_source(self, *, effective: bool = False) -> ReportingSourceCapabilitiesV1:
        source = self.producer._source
        if (
            id(source) != self._source_identity
            or not isinstance(source, ReportingProductionSource)
            or id(self.producer._object_reader) != self._reader_identity
            or not isinstance(self.producer._object_reader, ReportingSourceStagedObjectReader)
        ):
            raise failure("BINDING_MISMATCH")
        capabilities = ReportingSourceCapabilitiesV1.model_validate(
            source.capabilities.model_dump(mode="json", exclude_none=True)
        )
        if effective and capabilities.scope != "effective_account":
            raise failure("BINDING_MISMATCH")
        raw = self.wire()
        source_offering = capabilities.offering(self.source_offering_id)
        contract, key = source_offering.contract, self.verification_key
        source_values = contract.model_dump(mode="json")
        expected = {
            "report_definition_id": key.report_definition_id,
            "reporting_profile": key.reporting_profile,
        }
        expected.update(
            {
                name: getattr(key.definition, name)
                for name in (
                    "report_definition_uri",
                    "report_definition_sha256",
                    "schema_version",
                    "schema_uri",
                    "schema_sha256",
                    "schema_dialect",
                    "schema_ref_policy",
                )
            }
        )
        finality = raw["supported_finality"]
        configured = self.producer._offerings
        if (
            any(source_values.get(name) != value for name, value in expected.items())
            or source_offering.publication_namespace != configured.publication_namespace
            or dict(configured.source_scope) != capabilities.source_scope
            or not source_offering.provider_execution.supports_cancellation
            or (
                "official" in finality
                and (
                    not isinstance(source_offering, AuthoritativeOfferingV1)
                    or configured.official_offering_id != self.source_offering_id
                    or (
                        self.reconciled
                        and source_offering.correction_policy != "immutable_correction"
                    )
                )
            )
            or (
                "snapshot" in finality
                and (
                    not isinstance(source_offering, ProvisionalSnapshotOfferingV1)
                    or configured.snapshot_offering_id != self.source_offering_id
                )
            )
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        schedule = raw["schedule"]
        duration = iso_duration_milliseconds_v1(schedule["period_duration"])
        if not (
            iso_duration_milliseconds_v1(source_offering.windowing.minimum_window)
            <= duration
            <= iso_duration_milliseconds_v1(source_offering.windowing.maximum_window)
        ) or iso_duration_milliseconds_v1(schedule["delivery_sla"]) < iso_duration_milliseconds_v1(
            source_offering.worst_case_availability_lag
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        if isinstance(
            source_offering, ProvisionalSnapshotOfferingV1
        ) and duration < iso_duration_milliseconds_v1(source_offering.fastest_safe_cadence):
            raise failure("UNSUPPORTED_VERIFICATION")
        if (
            schedule["alignment"] == "source_timezone"
            and schedule.get("period_timezone") != source_offering.source_timezone
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        return capabilities

    def source_binding(
        self, configuration: ReportingConfiguration
    ) -> ReportingProductionSourceBinding:
        try:
            capabilities = self.check_source(effective=True)
            source = self.producer._source
            assert isinstance(source, ReportingProductionSource)
            binding = source.configuration_binding(configuration)
            if type(binding) is not ReportingProductionSourceBinding:
                raise failure("BINDING_MISMATCH")
            binding.check(configuration, capabilities, self.source_offering_id)
            return binding
        except Exception:
            raise failure("BINDING_MISMATCH") from None

    def configuration_schedule(self, configuration: ReportingConfiguration) -> dict[str, Any]:
        """Project a proven legacy clock into the public schedule vocabulary.

        Existing captured clocks and their closed storage decoder stay intact.
        New production admission requires the normative public phase; an old
        explicit anchor is compatible only when it produces the same periods.
        Calendar-month/year clocks unsupported by that decoder are refused.
        """
        offered = self.wire()["schedule"]
        schedule = configuration.schedule
        alignment = offered["alignment"]
        expected_alignment = (
            alignment if alignment in {"utc", "account_timezone"} else "custom_timezone"
        )
        zone, duration, anchor = _schedule_clock(schedule, configuration.account_timezone)
        if (
            schedule.alignment != expected_alignment
            or schedule.period_duration != offered["period_duration"]
            or schedule.delivery_sla != offered["delivery_sla"]
            or (alignment != "billing_cycle" and (anchor - datetime(1970, 1, 1)) % duration)
            or (alignment == "billing_cycle" and schedule.period_anchor is None)
        ):
            raise failure("BINDING_MISMATCH")
        result: dict[str, Any] = {
            "period_duration": schedule.period_duration,
            "delivery_sla": schedule.delivery_sla,
            "alignment": alignment,
        }
        if alignment in {"source_timezone", "billing_cycle"}:
            result["period_timezone"] = zone.key
        if alignment == "billing_cycle":
            assert schedule.period_anchor is not None
            result["period_anchor"] = schedule.period_anchor.isoformat()
        if (
            offered.get("period_timezone_policy") == "fixed"
            or offered.get("period_anchor_policy") == "fixed"
        ) and result.get("period_timezone") != offered.get("period_timezone"):
            raise failure("BINDING_MISMATCH")
        if offered.get(
            "period_anchor_policy"
        ) == "fixed" and schedule.period_anchor != aware_timestamp(offered["period_anchor"]):
            raise failure("BINDING_MISMATCH")
        if (
            alignment == "source_timezone"
            and zone.key
            != self.check_source(effective=True).offering(self.source_offering_id).source_timezone
        ):
            raise failure("BINDING_MISMATCH")
        return result

    def check_configuration(
        self,
        configuration: ReportingConfiguration,
        binding: ReportingDestinationBinding,
        *,
        retention_days: int,
    ) -> None:
        capabilities = self.check_source(effective=True)
        self.source_binding(configuration)
        self.configuration_schedule(configuration)
        raw, key = self.wire(), self.verification_key
        schedule, offered = configuration.schedule, raw["schedule"]
        if (
            binding.generation_key != configuration.generation_key
            or (
                configuration.report_definition_id,
                configuration.reporting_profile,
                configuration.feed_purpose,
                configuration.required_finality,
            )
            != (
                key.report_definition_id,
                key.reporting_profile,
                raw["feed_purpose"],
                raw["supported_finality"][0],
            )
            or not _same_definition(key, configuration.definition)
            or configuration.authoritative_party != "seller"
            or binding.reconciliation_mode != raw["reconciliation_mode"]
            or binding.feed_purpose != raw["feed_purpose"]
            or (binding.method, binding.transport, binding.format, binding.verification_profile)
            != (
                key.capability.method,
                key.capability.transport,
                key.capability.format,
                key.capability.verification_profile,
            )
            or binding.resource_retention_days != retention_days
            or binding.reader_compatibility != tuple(raw["method"].get("reader_compatibility", ()))
            or schedule.period_duration != offered["period_duration"]
            or iso_duration_to_timedelta(schedule.delivery_sla)
            != iso_duration_to_timedelta(offered["delivery_sla"])
            or iso_duration_milliseconds_v1(schedule.delivery_sla)
            < iso_duration_milliseconds_v1(
                capabilities.offering(self.source_offering_id).worst_case_availability_lag
            )
            or (
                offered.get("period_anchor_policy") == "fixed"
                and (
                    schedule.period_anchor is None
                    or schedule.period_anchor != aware_timestamp(str(offered.get("period_anchor")))
                )
            )
            or (
                offered.get("period_timezone_policy") == "fixed"
                and schedule.period_timezone != offered.get("period_timezone")
            )
        ):
            raise failure("BINDING_MISMATCH")
