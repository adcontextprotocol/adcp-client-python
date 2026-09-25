"""Provider declarations and trusted, immutable source generation bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.delivery_models import ReportingDestinationBinding
from adcp.reporting.ledger.models import ReportingConfiguration, ReportingConfigurationGenerationKey
from adcp.reporting.ledger.store import _config_payload
from adcp.reporting.materializer.contracts import ReportingWriterCapability, failure
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    ReportingConstituent,
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutor,
)


@dataclass(frozen=True)
class ReportingProductionMethod:
    """The actual provider's complete public method for one writer capability.

    This includes the provider, destination modes, access/producer identity and
    reader requirements. A method advertised by an offering must equal this
    declaration. The copied bytes cannot change through an adopter's mapping.
    Runtime writer, resolver, verifier and authorization checks remain required.
    """

    capability: ReportingWriterCapability
    method: Mapping[str, Any] = field(repr=False, compare=False)
    _wire: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        from adcp.validation.schema_loader import get_named_validator

        raw = json.loads(canonical_json_utf8_v1(dict(self.method)))
        validator = get_named_validator("core/reporting-delivery-offering.json")
        if (
            validator is None
            or next(
                validator.evolve(schema=validator.schema["properties"]["method"]).iter_errors(raw),
                None,
            )
            is not None
            or raw.get("orchestration") != "producer_managed"
            or (raw.get("pattern"), raw.get("transport"), raw.get("format"))
            != (self.capability.method, self.capability.transport, self.capability.format)
        ):
            raise ValueError("production method must match the provider's exact writer capability")
        object.__setattr__(self, "_wire", canonical_json_utf8_v1(raw))

    def wire(self) -> dict[str, Any]:
        return dict(json.loads(self._wire))


@dataclass(frozen=True)
class ReportingProductionDestinationBinding:
    """The provider's authorized, immutable destination contract for a caller.

    ``configuration`` is the complete secret-free public delivery method,
    including the selected destination. The provider resolves it from its
    trusted binding, not from a buyer's claim. Credentials remain in the
    independently authorized write/readback sessions. The SDK freezes these
    bytes at admission and compares them again before materialization.
    """

    binding: ReportingDestinationBinding = field(repr=False)
    method: ReportingProductionMethod
    configuration: Mapping[str, Any] = field(repr=False, compare=False)
    _wire: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        from adcp.reporting.evidence import consumer_reference, resource_location
        from adcp.validation.schema_loader import get_named_validator

        if (
            type(self.binding) is not ReportingDestinationBinding
            or type(self.method) is not ReportingProductionMethod
        ):
            raise ValueError("destination requires the exact provider binding and method")
        raw = json.loads(canonical_json_utf8_v1(dict(self.configuration)))
        validator = get_named_validator("core/reporting-delivery-method.json")
        offered = self.method.wire()
        if (
            validator is None
            or next(validator.iter_errors(raw), None) is not None
            or any(raw.get(k) != offered.get(k) for k in ("pattern", "transport", "orchestration"))
            or (raw["pattern"] == "file_transfer" and raw["format"] != offered.get("format"))
            or raw["destination"]["mode"] not in offered["destination_modes"]
        ):
            raise ValueError("destination must match the provider's complete method")
        destination = raw["destination"]
        if destination["mode"] == "existing":
            if destination["destination_ref"] != self.binding.destination_ref:
                raise ValueError("destination must match the immutable binding")
        elif destination.get("provider") != offered.get("provider") or destination.get(
            "access_mode"
        ) != offered.get("access_mode"):
            raise ValueError("destination must match the provider's complete method")
        if "location" in destination:
            resource_location(destination["location"])
        if "recipient" in destination:
            consumer_reference(destination["recipient"]["identity"])
        object.__setattr__(self, "_wire", canonical_json_utf8_v1(raw))

    def wire(self) -> dict[str, Any]:
        return dict(json.loads(self._wire))


@dataclass(frozen=True)
class ReportingProductionSourceBinding:
    """Trusted account-to-source mapping, fixed for a configuration generation.

    ``media_buy_products`` comes from the authenticated source/account mapping,
    never from buyer JSON or a report-definition identifier. The SDK persists
    it with admission, checks the effective capability digest on every source
    turn, and refuses a changed mapping. Reauthorization can withdraw a binding;
    it cannot silently change historical scope. No credentials belong here.
    """

    generation_key: ReportingConfigurationGenerationKey
    capabilities_sha256: str
    media_buy_products: tuple[tuple[str, str], ...]
    configuration_sha256: str = field(kw_only=True)

    def __post_init__(self) -> None:
        from adcp.reporting.evidence import reporting_identifier, sha256_value

        if type(self.generation_key) is not ReportingConfigurationGenerationKey:
            raise ValueError("source binding requires an exact configuration generation")
        sha256_value(self.capabilities_sha256)
        sha256_value(self.configuration_sha256)
        pairs = tuple(tuple(pair) for pair in self.media_buy_products)
        if any(len(pair) != 2 for pair in pairs):
            raise ValueError("source binding requires media-buy/product pairs")
        for media_buy_id, product_id in pairs:
            reporting_identifier(media_buy_id, maximum=255)
            reporting_identifier(product_id, maximum=255)
        if len({pair[0] for pair in pairs}) != len(pairs):
            raise ValueError("source binding media buys must be unique")
        object.__setattr__(self, "media_buy_products", tuple(sorted(pairs)))

    @classmethod
    def for_configuration(
        cls,
        configuration: ReportingConfiguration,
        *,
        capabilities_sha256: str,
        media_buy_products: tuple[tuple[str, str], ...],
    ) -> ReportingProductionSourceBinding:
        """Freeze the exact generation semantics and explicitly resolved products.

        Lifecycle changes retain the same semantic generation, matching the
        ledger's immutable configuration contract. Captured projection inputs
        independently retain each historical activation/deactivation boundary.
        """
        return cls(
            configuration.generation_key,
            capabilities_sha256,
            media_buy_products,
            configuration_sha256=hashlib.sha256(
                canonical_json_utf8_v1(_config_payload(configuration))
            ).hexdigest(),
        )

    def document(self) -> dict[str, Any]:
        key = self.generation_key
        return {
            "account_id": key.account_id,
            "delivery_config_id": key.delivery_config_id,
            "delivery_config_version": key.delivery_config_version,
            "capabilities_sha256": self.capabilities_sha256,
            "configuration_sha256": self.configuration_sha256,
            "media_buy_products": [list(pair) for pair in self.media_buy_products],
        }

    def check(
        self,
        configuration: ReportingConfiguration,
        capabilities: ReportingSourceCapabilitiesV1,
        offering_id: str,
    ) -> None:
        offering = capabilities.offering(offering_id)
        if (
            self.generation_key != configuration.generation_key
            or self.configuration_sha256
            != hashlib.sha256(canonical_json_utf8_v1(_config_payload(configuration))).hexdigest()
            or capabilities.scope != "effective_account"
            or self.capabilities_sha256 != capabilities.capabilities_sha256
            or {pair[0] for pair in self.media_buy_products} != set(configuration.media_buy_ids)
            or (self.media_buy_products and "media_buy" not in offering.constituent_kinds)
            or any(pair[1] not in offering.product_ids for pair in self.media_buy_products)
        ):
            raise failure("BINDING_MISMATCH")

    def constituents(self) -> tuple[ReportingConstituent, ...]:
        return tuple(
            MediaBuyConstituentV1(
                constituent_id=media_buy_id, media_buy_id=media_buy_id, product_id=product_id
            )
            for media_buy_id, product_id in self.media_buy_products
        )


@runtime_checkable
class ReportingProductionSource(ReportingSourceExecutor, Protocol):
    """A source with an authenticated generation mapping available before I/O.

    Discovery may precede any account binding. Admission and every source turn
    require the applicable binding, obtained from the source's trusted account
    configuration. Returning ``None`` withdraws authorization for new work.
    """

    def configuration_binding(
        self, configuration: ReportingConfiguration
    ) -> ReportingProductionSourceBinding | None: ...
