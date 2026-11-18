"""Atomic offerings bind public promises to the actual producer and verifier."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from adcp.reporting._source_authorization import require_account_work
from adcp.reporting._timestamp import aware_timestamp
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.inline_source import InlineReportingSource
from adcp.reporting.ledger.delivery_models import ReportingDestinationBinding
from adcp.reporting.ledger.finality import (
    ReportingStabilizedPolicy,
    finality_policy_source_class,
    stabilization_history_limit,
)
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    _schedule_clock,
    iso_duration_to_timedelta,
)
from adcp.reporting.ledger.producer import (
    ReportingProducer,
    _supports_official_source_formats,
    snapshot_restatement_cadence,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.contracts import ReportingVerificationKey, failure
from adcp.reporting.materializer.verification import _same_definition
from adcp.reporting.production.contracts import (
    ReportingProductionSource,
    ReportingProductionSourceBinding,
    _SourceAuthorizationRevokedError,
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

    def _source_offerings(
        self, capabilities: ReportingSourceCapabilitiesV1
    ) -> tuple[ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1, ...]:
        def configured_source(
            source_class: str,
        ) -> ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1:
            source_id = self.producer._offerings.offering_for(source_class)
            if source_id is None:
                raise failure("UNSUPPORTED_VERIFICATION")
            try:
                return capabilities.offering(source_id)
            except KeyError:
                raise failure("UNSUPPORTED_VERIFICATION") from None

        finalities = self.wire()["supported_finality"]
        snapshot = None
        if "snapshot" in finalities:
            candidate = configured_source("snapshot")
            if not isinstance(candidate, ProvisionalSnapshotOfferingV1):
                raise failure("UNSUPPORTED_VERIFICATION")
            snapshot = candidate
        if "official" not in finalities:
            if snapshot is None or self.source_offering_id != snapshot.offering_id:
                raise failure("UNSUPPORTED_VERIFICATION")
            if snapshot.official_close_lag is None:
                return (snapshot,)

        policy = self.verification_key.definition.finality_policy
        if policy is None:
            raise failure("UNSUPPORTED_VERIFICATION")
        source_class = finality_policy_source_class(policy)
        proof_source = configured_source(source_class)
        if not _supports_official_source_formats(proof_source) or (
            snapshot is not None and not _supports_official_source_formats(snapshot)
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        if self.source_offering_id != proof_source.offering_id and (
            snapshot is None or self.source_offering_id != snapshot.offering_id
        ):
            raise failure("UNSUPPORTED_VERIFICATION")
        if source_class == "official":
            if not isinstance(proof_source, AuthoritativeOfferingV1) or (
                self.reconciled and proof_source.correction_policy != "immutable_correction"
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
        elif not isinstance(proof_source, ProvisionalSnapshotOfferingV1):
            raise failure("UNSUPPORTED_VERIFICATION")
        if isinstance(policy, ReportingStabilizedPolicy):
            try:
                self.producer._stabilization_store()
                assert isinstance(proof_source, ProvisionalSnapshotOfferingV1)
                stabilization_history_limit(
                    policy,
                    snapshot_restatement_cadence(
                        proof_source, self.wire()["schedule"]["period_duration"]
                    ),
                )
            except LedgerConflictError:
                raise failure("UNSUPPORTED_VERIFICATION") from None
        if snapshot is None or snapshot.offering_id == proof_source.offering_id:
            return (proof_source,)
        return (snapshot, proof_source)

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
        configured = self.producer._offerings
        if dict(configured.source_scope) != capabilities.source_scope:
            raise failure("UNSUPPORTED_VERIFICATION")
        key = self.verification_key
        expected_contract = {
            "report_definition_id": key.report_definition_id,
            "reporting_profile": key.reporting_profile,
            **key.definition.to_wire(),
        }
        schedule = self.wire()["schedule"]
        duration = iso_duration_milliseconds_v1(schedule["period_duration"])
        sla = iso_duration_milliseconds_v1(schedule["delivery_sla"])
        for offering in self._source_offerings(capabilities):
            if isinstance(source, InlineReportingSource) and not source.supports_output_format(
                offering.formats
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
            if (
                any(
                    getattr(offering.contract, name) != value
                    for name, value in expected_contract.items()
                )
                or offering.publication_namespace != configured.publication_namespace
                or not offering.provider_execution.supports_cancellation
                or not (
                    iso_duration_milliseconds_v1(offering.windowing.minimum_window)
                    <= duration
                    <= iso_duration_milliseconds_v1(offering.windowing.maximum_window)
                )
                or sla < iso_duration_milliseconds_v1(offering.worst_case_availability_lag)
                or (
                    isinstance(offering, ProvisionalSnapshotOfferingV1)
                    and duration < iso_duration_milliseconds_v1(offering.fastest_safe_cadence)
                )
                or (
                    schedule["alignment"] == "source_timezone"
                    and schedule.get("period_timezone") != offering.source_timezone
                )
            ):
                raise failure("UNSUPPORTED_VERIFICATION")
        return capabilities

    def source_binding(
        self, configuration: ReportingConfiguration
    ) -> ReportingProductionSourceBinding:
        require_account_work(configuration.account_id)
        try:
            capabilities = self.check_source(effective=True)
            source = self.producer._source
            assert isinstance(source, ReportingProductionSource)
            binding = source.configuration_binding(configuration)
            if binding is None:
                # Discovery can probe several generations before selecting a
                # dispatch. Refuse this candidate without revoking a turn that
                # may select another one; the producer's dispatch/publication
                # checks own the account-wide stop after a live denial.
                raise _SourceAuthorizationRevokedError()
            if type(binding) is not ReportingProductionSourceBinding:
                raise failure("BINDING_MISMATCH")
            for offering in self._source_offerings(capabilities):
                binding.check(configuration, capabilities, offering.offering_id)
            return binding
        except _SourceAuthorizationRevokedError:
            raise
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
            )
            != (
                key.report_definition_id,
                key.reporting_profile,
                raw["feed_purpose"],
            )
            or configuration.required_finality not in raw["supported_finality"]
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
