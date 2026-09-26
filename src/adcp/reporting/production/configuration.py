"""A typed admission boundary for the application's existing account task."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from adcp.decisioning.capabilities import Account as AccountCapabilities
from adcp.reporting._timestamp import aware_timestamp
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDestinationBinding,
)
from adcp.reporting.ledger.models import ReportingConfiguration
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.contracts import failure
from adcp.server.base import ToolContext

if TYPE_CHECKING:
    from adcp.reporting.production.service import ReportingProductionSupport


@dataclass(frozen=True)
class ReportingConfigurationAdmission:
    """Resolved by trusted account/provider code, never decoded from buyer JSON.

    The bound references are opaque; credentials stay in destination sessions.
    The SDK validates the complete frozen tuple before admitting configuration.
    """

    offering_id: str
    configuration: ReportingConfiguration
    binding: ReportingDestinationBinding
    configuration_wire: Mapping[str, Any] = field(kw_only=True, repr=False, compare=False)
    _wire: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        from adcp.validation.schema_loader import get_named_validator

        wire = canonical_json_utf8_v1(dict(self.configuration_wire))
        validator = get_named_validator("core/reporting-delivery-config.json")
        if validator is None or next(validator.iter_errors(json.loads(wire)), None) is not None:
            raise ValueError("configuration must satisfy the complete public contract")
        object.__setattr__(self, "_wire", wire)

    def wire(self) -> dict[str, Any]:
        return dict(json.loads(self._wire))

    def check(self, support: ReportingProductionSupport) -> None:
        from adcp.reporting.ledger.store import reject_reserved_authoritative_party

        config, binding, raw = self.configuration, self.binding, self.wire()
        reject_reserved_authoritative_party(config)
        if raw.get("authoritative_party", "seller") != "seller":
            raise LedgerConflictError("UNSUPPORTED_FEATURE", "consumer authority is reserved")
        offering = support._configuration_offering(config, binding, offering_id=self.offering_id)
        schedule = offering.configuration_schedule(config)
        supplied_schedule = dict(raw["schedule"])
        if "period_anchor" in supplied_schedule:
            supplied_schedule["period_anchor"] = aware_timestamp(
                supplied_schedule["period_anchor"]
            ).isoformat()
            schedule["period_anchor"] = aware_timestamp(schedule["period_anchor"]).isoformat()
        scope = raw["scope"]
        # This producer freezes an explicit full media-buy denominator. An
        # application's dynamic all-buy or partial-coverage implementation must
        # not be represented as this installed exact-generation contract.
        if (
            raw["delivery_config_id"] != config.delivery_config_id
            or raw["delivery_config_version"] != config.delivery_config_version
            or raw["offering_id"] != self.offering_id
            or raw["feed_purpose"] != config.feed_purpose
            or raw["report_definition_id"] != config.report_definition_id
            or raw["reporting_profile"] != config.reporting_profile
            or raw["required_finality"] != config.required_finality
            or raw["reconciliation_mode"] != binding.reconciliation_mode
            or set(scope) != {"media_buy_ids"}
            or set(scope["media_buy_ids"]) != set(config.media_buy_ids)
            or raw["coverage_requirement"] != "full"
            or supplied_schedule != schedule
            or raw.get("method") != support._destination_binding(binding, offering).wire()
            or raw["active"] != (config.activated_at is not None and config.deactivated_at is None)
            or (
                "revocation_effective_at" in raw
                and aware_timestamp(raw["revocation_effective_at"]) != config.deactivated_at
            )
        ):
            raise failure("BINDING_MISMATCH")


ConfigurationAdmission = Callable[[ReportingConfigurationAdmission], Awaitable[None]]
ConfigurationTask = Callable[
    [dict[str, Any], ToolContext | None, ConfigurationAdmission], Awaitable[dict[str, Any]]
]


@dataclass(frozen=True)
class ReportingProductionConfigurationTask:
    """Compose the account implementation with enforced SDK reporting admission.

    ``handle`` remains the application's authenticated, caller-owned desired
    state account task. It resolves provider grants and opaque bindings, calls
    the supplied ``admit`` for every accepted reporting generation, and returns
    the normal account response. The wrapper verifies returned ready states
    against the actual stored records and that call's validated admissions.
    A task cannot return a successful unsupported promise without a matching
    admitted configuration, including on exact replay.
    """

    handle: ConfigurationTask = field(repr=False)
    account: AccountCapabilities = field(repr=False, compare=False)
    _account_wire: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        from adcp.validation.schema_loader import get_named_validator

        raw = self.account.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        validator = get_named_validator("protocol/get-adcp-capabilities-response.json")
        if (
            validator is None
            or next(
                validator.evolve(schema=validator.schema["properties"]["account"]).iter_errors(raw),
                None,
            )
            is not None
            or raw.get("account_financials") is True
            or any(
                raw.get(name, {}).get("supported") is True
                for name in ("notifications", "change_feed", "identity_updates")
            )
        ):
            raise ValueError(
                "configuration task requires its actual account capabilities and mounted operations"
            )
        object.__setattr__(self, "_account_wire", canonical_json_utf8_v1(raw))

    def account_capabilities(self) -> dict[str, Any]:
        return dict(json.loads(self._account_wire))

    async def execute(
        self,
        support: ReportingProductionSupport,
        request: dict[str, Any],
        context: ToolContext | None,
    ) -> dict[str, Any]:
        from adcp.validation.schema_loader import get_named_validator

        validator = get_named_validator("account/sync-accounts-request.json")
        if validator is None or next(validator.iter_errors(request), None) is not None:
            raise LedgerConflictError("INVALID_REQUEST", "account request is invalid")
        for entry in request["accounts"]:
            configurations = entry.get("reporting_delivery_configs", ())
            keys = [(c["delivery_config_id"], c["delivery_config_version"]) for c in configurations]
            if len(set(keys)) != len(keys):
                raise LedgerConflictError(
                    "INVALID_REQUEST", "configuration generations must be unique"
                )
        admitted: dict[tuple[str, str, int], ReportingConfigurationAdmission] = {}

        async def admit(value: ReportingConfigurationAdmission) -> None:
            if request.get("dry_run") is True:
                raise LedgerConflictError(
                    "INVALID_REQUEST", "dry runs cannot admit reporting state"
                )
            if type(value) is not ReportingConfigurationAdmission or context is None:
                raise LedgerConflictError("UNAUTHORIZED", "configuration authority is unavailable")
            who = await support.handler._authorize(
                {"account": {"account_id": value.configuration.account_id}}, context
            )
            if who != value.binding.principal:
                raise LedgerConflictError("UNAUTHORIZED", "configuration authority is unavailable")
            matched = False
            for entry in request["accounts"]:
                if "reporting_delivery_configs" not in entry or "account" not in entry:
                    continue
                try:
                    requested_caller = await support.handler._authorize(
                        {"account": entry["account"]}, context
                    )
                # An unresolved account cannot authorize this admission.
                except Exception:  # nosec B112
                    continue
                if requested_caller != who:
                    continue
                desired = entry["reporting_delivery_configs"]
                matched = value.wire() in desired or (
                    value.configuration.deactivated_at is not None
                    and not any(
                        (c["delivery_config_id"], c["delivery_config_version"])
                        == (
                            value.configuration.delivery_config_id,
                            value.configuration.delivery_config_version,
                        )
                        for c in desired
                    )
                )
                if matched:
                    break
            if not matched:
                raise LedgerConflictError(
                    "INVALID_REQUEST", "configuration differs from requested account state"
                )
            support.validate_configuration(value)
            await support._check_notifications(who.account_id)
            key = (
                who.account_id,
                value.configuration.delivery_config_id,
                value.configuration.delivery_config_version,
            )
            if key in admitted and admitted[key] != value:
                raise LedgerConflictError(
                    "CONFIGURATION_GENERATION_IMMUTABLE", "configuration identity conflicts"
                )
            await support._admit_configuration(value)
            # The caller already entered the migrated, drained production
            # lifecycle. Complete this account's versioned baseline before
            # echoing ready; adopters need no account-enumeration worker or
            # hand-maintained readiness flag. A failed activation remains
            # unready and can resume under the same durable identities.
            await support.activate(account_id=who.account_id)
            admitted[key] = value

        response = await self.handle(dict(request), context, admit)
        if not isinstance(response, Mapping):
            raise LedgerConflictError(
                "INVALID_REQUEST", "configuration task returned an invalid response"
            )
        if response.get("errors") or request.get("dry_run") is True:
            return dict(response)
        for account in response.get("accounts", ()):
            if account.get("action") == "failed":
                continue
            for state in account.get("reporting_delivery_configs", ()):
                if state.get("state") not in {"ready", "inactive"}:
                    continue
                config = state.get("configuration", {})
                key = (
                    account.get("account_id"),
                    config.get("delivery_config_id"),
                    config.get("delivery_config_version"),
                )
                value = admitted.get(key)
                if value is None:
                    raise LedgerConflictError(
                        "REPORTING_CONFIGURATION_UNADMITTED", "configuration requires SDK admission"
                    )
                validator = get_named_validator("core/reporting-delivery-config-state.json")
                if (
                    validator is None
                    or next(validator.iter_errors(state), None) is not None
                    or config != value.wire()
                ):
                    raise LedgerConflictError(
                        "REPORTING_CONFIGURATION_UNADMITTED",
                        "configuration state differs from admission",
                    )
                expected_state = (
                    "inactive" if value.configuration.deactivated_at is not None else "ready"
                )
                if state["state"] != expected_state:
                    raise LedgerConflictError(
                        "REPORTING_CONFIGURATION_UNADMITTED", "configuration lifecycle differs"
                    )
                for name in ("activated_at", "deactivated_at"):
                    actual_time = getattr(value.configuration, name)
                    supplied = state.get(name)
                    if supplied is not None and aware_timestamp(supplied) != actual_time:
                        raise LedgerConflictError(
                            "REPORTING_CONFIGURATION_UNADMITTED", "configuration lifecycle differs"
                        )
                coverage = state.get("current_coverage")
                if state["state"] == "ready" and (
                    not isinstance(coverage, dict)
                    or coverage["status"] != "full"
                    or set(coverage["media_buy_ids"]) != set(value.configuration.media_buy_ids)
                    or set(coverage["fully_covered_media_buy_ids"])
                    != set(value.configuration.media_buy_ids)
                    or any(
                        coverage[k]
                        for k in (
                            "partially_covered_media_buy_ids",
                            "unsupported_media_buy_ids",
                            "unknown_media_buy_ids",
                            "unsupported_package_ids",
                            "unknown_package_ids",
                            "limitations",
                        )
                    )
                    or set(coverage["package_ids"]) != set(coverage["covered_package_ids"])
                ):
                    raise LedgerConflictError(
                        "REPORTING_CONFIGURATION_UNADMITTED", "configuration coverage differs"
                    )
                if state["state"] == "inactive":
                    from adcp.reporting.ledger.models import derive_period, first_ordinal_after

                    configuration = value.configuration
                    assert configuration.deactivated_at is not None
                    ordinal = first_ordinal_after(
                        configuration.schedule,
                        account_timezone=configuration.account_timezone,
                        activated_at=configuration.deactivated_at,
                    )
                    cutoff = derive_period(
                        configuration.schedule,
                        account_timezone=configuration.account_timezone,
                        ordinal=ordinal,
                    ).start
                    if aware_timestamp(state["publication_stopped_at"]) != cutoff:
                        raise LedgerConflictError(
                            "REPORTING_CONFIGURATION_UNADMITTED", "configuration cutoff differs"
                        )
                who = ReportingDeliveryPrincipal(
                    value.configuration.account_id, value.binding.consumer_id
                )
                actual = await support.store.get_destination_binding(
                    caller=who, generation_key=value.configuration.generation_key
                )
                configs = await support.store.list_configurations(account_id=who.account_id)
                support.validate_configuration(value)
                if actual != value.binding or value.configuration not in configs:
                    raise LedgerConflictError(
                        "REPORTING_CONFIGURATION_UNADMITTED",
                        "configuration admission is unavailable",
                    )
                if state.get("destination_ref") != actual.destination_ref:
                    raise LedgerConflictError(
                        "REPORTING_CONFIGURATION_UNADMITTED", "configuration destination differs"
                    )
        return dict(response)
