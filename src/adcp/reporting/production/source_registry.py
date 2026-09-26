"""Fixed execution profiles and durable, secret-free service admission facts.

This is an opt-in bridge to existing production producers. It does not discover
accounts, supply a new scheduler, or replace an adapter's live authorization.
Profiles are fixed before startup; heterogeneous profiles require distinct B2
offerings/producers. Retry deadlines remain owned by the acquisition protocol.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import ReportingConfiguration
    from adcp.reporting.ledger.producer import ProducerOfferings, ReportingProducer
    from adcp.reporting.production.offerings import ReportingProductionOffering
    from adcp.reporting.service import ReportingContextResolver
    from adcp.reporting.source import ReportingSourceCapabilitiesV1


def _profile(value: ProducerOfferings) -> dict[str, Any]:
    return {
        "snapshot_offering_id": value.snapshot_offering_id,
        "official_offering_id": value.official_offering_id,
        "publication_namespace": value.publication_namespace,
        "requested_metrics": list(value.requested_metrics),
        "requested_dimensions": list(value.requested_dimensions),
        "currency": value.currency,
        "source_scope": value.source_scope,
        # Policy, not a source execution key or an absolute retry deadline.
        "slice_timeout_microseconds": value.slice_timeout // timedelta(microseconds=1),
    }


@dataclass(frozen=True)
class ReportingProductionSourceContext:
    """Canonical version-one facts; credentials must never be placed in scope.

    Construction is internal to the registry. Persisted input is accepted only
    by comparison with its closed expected document, not by a permissive codec.
    """

    _wire: bytes = field(repr=False)

    def document(self) -> dict[str, Any]:
        return dict(json.loads(self._wire))


@dataclass(frozen=True)
class _Registration:
    adapter: str
    producer: ReportingProducer = field(repr=False, compare=False)
    profile: bytes = field(repr=False)


class ReportingProductionSourceRegistry:
    """Bind stable adapter names to the actual fixed production producers.

    ``account_context`` runs only at authenticated configuration admission,
    outside the storage transaction. Recovery validates the selected persisted
    generation against these profiles and the live production source binding;
    it never calls the resolver or rebuilds an in-memory account catalog.
    """

    def __init__(self, *, account_context: ReportingContextResolver) -> None:
        self._resolve = account_context
        self._entries: dict[str, _Registration] = {}
        self._frozen = False

    def register(self, adapter: str, producer: ReportingProducer) -> None:
        if self._frozen:
            raise ValueError("production source registry is frozen")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", adapter):
            raise ValueError("adapter must be a stable identifier")
        if adapter in self._entries or any(v.producer is producer for v in self._entries.values()):
            raise ValueError("production source profile must have one unambiguous registration")
        self._entries[adapter] = _Registration(
            adapter, producer, canonical_json_utf8_v1(_profile(producer._offerings))
        )

    def freeze(self, offerings: tuple[ReportingProductionOffering, ...]) -> None:
        for offering in offerings:
            self._registration(offering)
        if any(
            not any(entry.producer is offering.producer for offering in offerings)
            for entry in self._entries.values()
        ):
            raise ValueError("registered production source profile has no offering")
        self._frozen = True

    def _registration(self, offering: ReportingProductionOffering) -> _Registration:
        matches = [v for v in self._entries.values() if v.producer is offering.producer]
        if len(matches) != 1:
            raise ValueError("production source profile is not registered")
        entry = matches[0]
        if entry.profile != canonical_json_utf8_v1(_profile(entry.producer._offerings)):
            raise ValueError("production source profile changed")
        return entry

    def _expected(
        self,
        configuration: ReportingConfiguration,
        offering: ReportingProductionOffering,
        capabilities: ReportingSourceCapabilitiesV1,
    ) -> dict[str, Any]:
        entry = self._registration(offering)
        return {
            "version": 1,
            "adapter": entry.adapter,
            "account_id": configuration.account_id,
            "account_timezone": configuration.account_timezone,
            "offering_id": offering.offering_id,
            "source_offering_id": offering.source_offering_id,
            "capabilities_sha256": capabilities.capabilities_sha256,
            **json.loads(entry.profile),
        }

    async def resolve(
        self,
        configuration: ReportingConfiguration,
        offering: ReportingProductionOffering,
        capabilities: ReportingSourceCapabilitiesV1,
    ) -> ReportingProductionSourceContext:
        from adcp.reporting.service import ReportingAccountContext, _thaw

        context = self._resolve(configuration)
        if inspect.isawaitable(context):
            context = await context
        expected = self._expected(configuration, offering, capabilities)
        if (
            type(context) is not ReportingAccountContext
            or context.account_id != configuration.account_id
            or context.account_timezone != configuration.account_timezone
            or context.adapter != expected["adapter"]
            or canonical_json_utf8_v1(_profile(context.producer_offerings()))
            != self._registration(offering).profile
            or (
                context.capability_offering
                and canonical_json_utf8_v1(_thaw(context.capability_offering))
                != canonical_json_utf8_v1(offering.wire())
            )
        ):
            raise ValueError("resolved account context does not match its fixed production profile")
        return ReportingProductionSourceContext(canonical_json_utf8_v1(expected))

    def recover(
        self,
        configuration: ReportingConfiguration,
        offering: ReportingProductionOffering,
        capabilities: ReportingSourceCapabilitiesV1,
        document: dict[str, Any] | None,
    ) -> ReportingProductionSourceContext:
        if document is None:
            raise ValueError("legacy service source context requires explicit migration")
        expected = canonical_json_utf8_v1(self._expected(configuration, offering, capabilities))
        if expected != canonical_json_utf8_v1(document):
            raise ValueError(
                "persisted account context does not match its fixed production profile"
            )
        return ReportingProductionSourceContext(expected)
