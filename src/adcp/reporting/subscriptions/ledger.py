"""Write a reporting subscription's owed set into the reporting ledger.

The subscription engine decides which periods a subscription owes an account
(:meth:`~adcp.reporting.subscriptions.ReportingSubscriptionEngine.frozen_owed_sets`).
:class:`SubscriptionObligationPlanner` makes them ordinary ledger obligations,
so the producer executes them, ``get_reporting_status`` reports them and the
consumer-status loop accepts statements about them.

Identity (aligned with the TypeScript SDK): one configuration generation per
(account, owning principal, ``subscription_id``, ``subscription_version``,
resolved ``offering_id``). ``delivery_config_id`` is the ``subscription_id``
and ``delivery_config_version`` the ``subscription_version``; the generation
key carries the ``offering_id``, so several offerings of one version (``match:
all``, per-product resolution) never collide. Account-level generations keep
their identity exactly.

Each generation holds an owed set, never a period grid:

* obligations are written only for periods the engine froze (ended by the
  planning horizon), each with its own period, the ``media_buy_ids`` owed that
  period (frozen at ``scope_resolved_at``, the period end) and ``expected_at``
  = period end + the offering's ``delivery_sla``;
* obligations are immutable: a later cycle never rewrites or removes one;
* after the obligations are durable, the owed set's completeness horizon
  advances on every generation the subscription owns on the account, and a
  ``get_reporting_status`` scope ending after it is not closed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.currency import validate_currency
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDefinitionBinding,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    ReportingScheduleSpec,
    ReportingSubscriptionGeneration,
)
from adcp.reporting.ledger.store import ReportingLedgerStore, ReportingSubscriptionLedger
from adcp.reporting.subscriptions._clock import parse_delivery_sla, utc_instant
from adcp.reporting.subscriptions.coverage import OwedSubscriptionPeriod
from adcp.reporting.subscriptions.resolution import SubscriptionOffering, _text, _value

#: The frozen reporting currency of one generation, as its account context gives it.
CurrencyResolver = Callable[[ReportingConfiguration], str]
#: Installs one generation through the host's trusted path (for example
#: ``ReliableReportingService``, which resolves and validates the account
#: context and persists the generation). Defaults to ``put_configuration``.
GenerationInstaller = Callable[[ReportingConfiguration, Mapping[str, Any]], Awaitable[None]]
#: Commits one owed obligation of a generation; production commits it with the
#: constituents it freezes from the host's current pool. Defaults to
#: ``commit_obligation``.
ObligationCommitter = Callable[
    [ReportingConfiguration, ReportingObligationRecord], Awaitable[ReportingObligationRecord]
]


@dataclass(frozen=True)
class FrozenOwedSet:
    """One subscription's frozen owed set on one account.

    ``periods`` are every owed period ending by ``through``; ``versions`` maps
    each retained ``subscription_version`` to its configuration, which supplies
    the generation's ``feed_purpose`` and ``required_finality``.
    """

    principal_id: str
    account_id: str
    subscription_id: str
    through: datetime
    periods: tuple[OwedSubscriptionPeriod, ...]
    versions: Mapping[int, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubscriptionPlanResult:
    generations: tuple[ReportingConfigurationGenerationKey, ...] = ()
    obligations_written: tuple[str, ...] = ()
    through: datetime | None = None


def _iso(value: datetime) -> str:
    return utc_instant(value).isoformat()


def _parse(value: str) -> datetime:
    return utc_instant(datetime.fromisoformat(value.replace("Z", "+00:00")))


def subscription_obligation_id(
    *,
    account_id: str,
    principal_id: str,
    subscription_id: str,
    subscription_version: int,
    offering_id: str,
    period_start: datetime,
    period_end: datetime,
) -> str:
    """The deterministic id of one subscription obligation."""
    digest = hashlib.sha256(
        canonical_json_utf8_v1(
            [
                "subscription",
                account_id,
                principal_id,
                subscription_id,
                subscription_version,
                offering_id,
                _iso(period_start),
                _iso(period_end),
            ]
        )
    ).hexdigest()
    return f"rpo_{digest[:40]}"


def subscription_schedule(offering: SubscriptionOffering, zone: str) -> ReportingScheduleSpec:
    """The ledger's record of an offering's schedule identity, as resolved for one zone.

    Kept verbatim for readback; owed-set generations never derive periods from
    it (the engine's calendar did).
    """
    if offering.alignment == "utc":
        return ReportingScheduleSpec(
            period_duration=offering.period_duration,
            delivery_sla=offering.delivery_sla,
            alignment="utc",
        )
    if offering.alignment == "account_timezone":
        return ReportingScheduleSpec(
            period_duration=offering.period_duration,
            delivery_sla=offering.delivery_sla,
            alignment="account_timezone",
        )
    return ReportingScheduleSpec(
        period_duration=offering.period_duration,
        delivery_sla=offering.delivery_sla,
        alignment="custom_timezone",
        period_timezone=zone,
        period_anchor=offering.period_anchor if offering.alignment == "billing_cycle" else None,
    )


class SubscriptionObligationPlanner:
    """Install owed-set generations and write their frozen obligations.

    ``currency`` gives each generation's frozen reporting currency (a code,
    or a resolver called after the generation is installed). ``definitions``
    binds offerings to their content-addressed report definitions; a seller
    advertising ``reporting.core`` supplies them. ``install`` replaces the
    plain ``put_configuration`` with the host's trusted installation path.
    """

    def __init__(
        self,
        store: ReportingLedgerStore,
        *,
        offerings: Sequence[SubscriptionOffering],
        currency: str | CurrencyResolver,
        definitions: Mapping[str, ReportingDefinitionBinding] | None = None,
        automated_recovery_window: timedelta = timedelta(hours=6),
        status_retention_days: int = 400,
        install: GenerationInstaller | None = None,
        commit: ObligationCommitter | None = None,
    ) -> None:
        if not isinstance(store, ReportingSubscriptionLedger):
            raise TypeError(
                "this ledger store cannot hold reporting-subscription owed sets; it needs "
                "list_generation_obligations, record_owed_set_progress and owed_set_through"
            )
        self._store = store
        self._ledger = store
        self._offerings = {item.offering_id: item for item in offerings}
        self._currency = currency
        self._definitions = dict(definitions or {})
        self._window = automated_recovery_window
        self._retention_days = status_retention_days
        self._install = install or self._put_configuration
        self._commit = commit or self._commit_obligation

    async def _put_configuration(
        self, configuration: ReportingConfiguration, version: Mapping[str, Any]
    ) -> None:
        del version
        await self._store.put_configuration(configuration)

    async def _commit_obligation(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> ReportingObligationRecord:
        del configuration
        return await self._store.commit_obligation(obligation)

    def _version(self, owed: FrozenOwedSet, version: int) -> Mapping[str, Any]:
        configuration = owed.versions.get(version)
        if configuration is None:
            raise ValueError(
                f"subscription {owed.subscription_id!r} version {version} has no known "
                "configuration"
            )
        if hasattr(configuration, "model_dump"):
            dumped: Mapping[str, Any] = configuration.model_dump(mode="json", exclude_none=True)
            return dumped
        return dict(configuration)

    def _currency_for(self, configuration: ReportingConfiguration) -> str:
        value = self._currency if isinstance(self._currency, str) else self._currency(configuration)
        return validate_currency(value)

    def _configuration(
        self,
        owed: FrozenOwedSet,
        version: int,
        offering: SubscriptionOffering,
        zone: str,
    ) -> ReportingConfiguration:
        configuration = owed.versions.get(version)
        if configuration is None:
            raise ValueError(
                f"subscription {owed.subscription_id!r} version {version} has no known "
                "configuration"
            )
        if hasattr(configuration, "model_dump"):
            configuration = configuration.model_dump(mode="json", exclude_none=True)
        selector = _value(configuration, "selector")
        return ReportingConfiguration(
            delivery_config_id=owed.subscription_id,
            delivery_config_version=version,
            account_id=owed.account_id,
            consumer_id=owed.principal_id,
            report_definition_id=offering.report_definition_id,
            reporting_profile=offering.reporting_profile,
            feed_purpose=_text(_value(selector, "feed_purpose")),
            schedule=subscription_schedule(offering, zone),
            required_finality=_text(_value(selector, "required_finality")),  # type: ignore[arg-type]
            account_timezone=zone,
            activated_at=None,
            automated_recovery_window=self._window,
            status_retention_days=self._retention_days,
            definition=self._definitions.get(offering.offering_id),
            subscription=ReportingSubscriptionGeneration(
                owed.subscription_id, offering.offering_id
            ),
        )

    async def plan(
        self, owed: FrozenOwedSet, *, through: datetime | None = None
    ) -> SubscriptionPlanResult:
        """Install generations, write new obligations, then advance completeness.

        ``through`` caps the engine's horizon (a service passes its cycle
        clock), so nothing past it is written or claimed complete.

        Idempotent: a repeated cycle writes nothing new. A generation whose
        content would change (an offering edited under the same id) raises the
        store's immutability conflict instead of silently diverging.
        """
        horizon = utc_instant(owed.through, field="through")
        if through is not None:
            horizon = min(horizon, utc_instant(through, field="through"))
        through = horizon
        installed: dict[ReportingConfigurationGenerationKey, ReportingConfiguration] = {}
        written: list[str] = []
        for period in owed.periods:
            start, end = _parse(period.period_start), _parse(period.period_end)
            if end > through:
                continue  # not frozen yet: its buys may still change
            offering = self._offerings.get(period.offering_id)
            if offering is None:
                raise ValueError(f"offering {period.offering_id!r} is not registered")
            configuration = self._configuration(
                owed, period.delivery_config_version, offering, period.source_timezone
            )
            key = configuration.generation_key
            if key not in installed:
                await self._install(
                    configuration, self._version(owed, period.delivery_config_version)
                )
                installed[key] = configuration
            existing = await self._store.find_obligation(
                account_id=owed.account_id,
                consumer_id=owed.principal_id,
                delivery_config_id=owed.subscription_id,
                delivery_config_version=period.delivery_config_version,
                period_start=start,
                period_end=end,
                offering_id=offering.offering_id,
            )
            if existing is not None:
                continue  # immutable: never rewritten
            expected_at = end + parse_delivery_sla(offering.delivery_sla)
            obligation = ReportingObligationRecord(
                reporting_obligation_id=subscription_obligation_id(
                    account_id=owed.account_id,
                    principal_id=owed.principal_id,
                    subscription_id=owed.subscription_id,
                    subscription_version=period.delivery_config_version,
                    offering_id=offering.offering_id,
                    period_start=start,
                    period_end=end,
                ),
                account_id=owed.account_id,
                consumer_id=owed.principal_id,
                delivery_config_id=owed.subscription_id,
                delivery_config_version=period.delivery_config_version,
                report_definition_id=offering.report_definition_id,
                reporting_profile=offering.reporting_profile,
                feed_purpose=configuration.feed_purpose,
                period=ReportingPeriodBoundary(
                    period_key=(
                        f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}_{offering.period_duration}"
                    ),
                    start=start,
                    end=end,
                    source_timezone=period.source_timezone,
                    expected_at=expected_at,
                ),
                scope_resolved_at=end,
                media_buy_ids=tuple(period.media_buy_ids),
                required_finality=configuration.required_finality,
                automated_recovery_deadline_at=expected_at + self._window,
                schedule=configuration.schedule,
                definition=configuration.definition,
                currency=self._currency_for(configuration),
                offering_id=offering.offering_id,
                # Planned at the horizon, never ahead of the planning clock.
                created_at=through,
            )
            stored = await self._commit(configuration, obligation)
            written.append(stored.reporting_obligation_id)
        owned = [
            item.generation_key
            for item in await self._store.list_configurations(
                caller=ReportingDeliveryPrincipal(owed.account_id, owed.principal_id),
                delivery_config_ids=[owed.subscription_id],
            )
            if item.owed_set
        ]
        # Completeness advances only after every obligation is durable.
        for key in owned:
            await self._ledger.record_owed_set_progress(key, through)
        return SubscriptionPlanResult(
            generations=tuple(owned), obligations_written=tuple(written), through=through
        )


__all__ = [
    "CurrencyResolver",
    "FrozenOwedSet",
    "GenerationInstaller",
    "ObligationCommitter",
    "SubscriptionObligationPlanner",
    "SubscriptionPlanResult",
    "subscription_obligation_id",
    "subscription_schedule",
]
