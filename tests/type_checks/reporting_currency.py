"""Trusted account currency callbacks and explicit low-level obligation writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol

from typing_extensions import assert_type

from adcp.reporting.ledger import (
    CurrencyResolver,
    FixedCurrencyResolver,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDefinitionBinding,
    ReportingLedgerStore,
    ReportingObligationRecord,
    ReportingProducer,
    require_single_currency,
    validate_currency,
)
from adcp.reporting.source import ReportingSourceExecutor


class TrustedAccountHistory(Protocol):
    async def currencies_for_scope(
        self,
        *,
        generation: ReportingConfigurationGenerationKey,
        at: datetime,
        media_buy_ids: tuple[str, ...],
        package_ids: tuple[str, ...],
    ) -> tuple[str, ...]: ...


def configure(
    store: ReportingLedgerStore, source: ReportingSourceExecutor, history: TrustedAccountHistory
) -> tuple[ReportingProducer, ReportingProducer, ReportingProducer]:
    # A later ReliableReportingService account-context resolver can implement
    # this lookup. No buyer context or source response participates.
    async def historical_currency(
        configuration: ReportingConfiguration, candidate: ReportingObligationRecord
    ) -> str:
        assert_type(candidate.currency, str | None)
        values = await history.currencies_for_scope(
            generation=configuration.generation_key,
            at=candidate.scope_resolved_at,
            media_buy_ids=candidate.media_buy_ids,
            package_ids=candidate.package_ids,
        )
        return assert_type(require_single_currency(values), str)

    def synchronous_currency(
        configuration: ReportingConfiguration, candidate: ReportingObligationRecord
    ) -> str:
        return {"us-account": "USD", "eu-account": "EUR"}[candidate.account_id]

    async_resolver: CurrencyResolver = historical_currency
    sync_resolver: CurrencyResolver = synchronous_currency
    fixed_resolver: CurrencyResolver = FixedCurrencyResolver("EUR")
    offerings = ProducerOfferings(snapshot_offering_id="SNAPSHOT")
    return (
        ReportingProducer(
            store=store, source=source, offerings=offerings, currency_resolver=async_resolver
        ),
        ReportingProducer(
            store=store, source=source, offerings=offerings, currency_resolver=sync_resolver
        ),
        ReportingProducer(
            store=store, source=source, offerings=offerings, currency_resolver=fixed_resolver
        ),
    )


async def explicit_low_level_currency(
    store: ReportingLedgerStore,
    candidate: ReportingObligationRecord,
    verified_definition: ReportingDefinitionBinding,
) -> None:
    # Unit declarations are derived from the verified content-addressed
    # definition by trusted seller code, then retained with the obligation.
    definition = replace(
        verified_definition,
        monetary_metric_units=(("spend", "EUR"),),
        monetary_control_total_units=(("spend", "EUR"),),
    )
    frozen = replace(candidate, definition=definition, currency=validate_currency("EUR"))
    assert_type(await store.commit_obligation(frozen), ReportingObligationRecord)
    assert_type(frozen.currency, str | None)  # None remains representable for legacy reads.
