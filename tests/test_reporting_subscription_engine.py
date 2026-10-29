"""Seller runtime for reporting subscriptions, and buyer/seller conformance.

The conformance cases follow adcontextprotocol/adcp-client#3504: the buyer
computes its expected periods with ``expected_subscription_periods`` from what
it can see (the ``get_principal`` readback, ``subscription_coverage[]``, its own
buys and grant history), and the seller's owed periods must equal them period
by period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest

from adcp.reporting.subscriptions import (
    AccountDeliveryConfig,
    AuthorizedAccount,
    AuthorizedAccountPage,
    ReportingSubscriptionEngine,
    ReportingSubscriptionSupport,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionBuyFlight,
    SubscriptionBuyPackage,
    SubscriptionOffering,
    expected_subscription_periods,
)
from adcp.server import PrincipalChange, PrincipalIdentity, PrincipalService

IDENTITY = PrincipalIdentity("https://agency.example/agent", "buyer_agent")
NY = "America/New_York"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def offering(offering_id: str, dimensions: tuple[str, ...], **kwargs) -> SubscriptionOffering:
    defaults = dict(
        report_definition_id=f"def-{offering_id}",
        reporting_profile="delivery",
        feed_purpose="analytics",
        period_duration="P1D",
        alignment="account_timezone",
        delivery_sla="PT6H",
        supported_finality=frozenset({"official"}),
        reconciliation_mode="delivery_only",
    )
    defaults.update(kwargs)
    return SubscriptionOffering(
        offering_id=offering_id, dimensions=frozenset(dimensions), **defaults
    )


PACKAGE = offering("pkg", ("package",))
PACKAGE_CREATIVE = offering("pkg-creative", ("package", "creative"))
PACKAGE_COUNTRY = offering("pkg-country", ("package", "country"))
HOURLY = offering(
    "pkg-hourly", ("package",), period_duration="PT1H", alignment="utc", delivery_sla="PT2H"
)
OFFERINGS = [PACKAGE, PACKAGE_CREATIVE, PACKAGE_COUNTRY, HOURLY]
ALL_IDS = tuple(item.offering_id for item in OFFERINGS)


def subscription(subscription_id: str = "analytics", **overrides) -> dict:
    payload = {
        "subscription_id": subscription_id,
        "subscription_version": 1,
        "active": True,
        "scope": {"all_authorized_accounts": True},
        "selector": {
            "feed_purpose": "analytics",
            "period_duration": "P1D",
            "required_finality": "official",
            "dimensions": ["package"],
        },
        "reconciliation_mode": "delivery_only",
    }
    payload.update(overrides)
    return payload


def buy(
    media_buy_id: str,
    start: str,
    end: str,
    *,
    confirmed: str | None = None,
    offering_ids: tuple[str, ...] = ALL_IDS,
    **kwargs,
) -> SubscriptionBuy:
    return SubscriptionBuy(
        media_buy_id=media_buy_id,
        confirmed_at=at(confirmed or start),
        start_time=at(start),
        end_time=at(end),
        reporting_delivery_offering_ids=offering_ids,
        **kwargs,
    )


@dataclass
class Host:
    accounts: dict[str, AuthorizedAccount] = field(default_factory=dict)
    buys: dict[str, list[SubscriptionBuy]] = field(default_factory=dict)
    configs: dict[str, list[AccountDeliveryConfig]] = field(default_factory=dict)
    page_size: int = 2

    async def list_authorized_accounts(self, principal, *, cursor):
        ids = sorted(self.accounts)
        start = int(cursor or 0)
        page = ids[start : start + self.page_size]
        following = start + self.page_size
        return AuthorizedAccountPage(
            accounts=tuple(self.accounts[item] for item in page),
            next_cursor=str(following) if following < len(ids) else None,
        )

    async def account_buys(self, principal, account_id):
        return list(self.buys.get(account_id, ()))

    async def account_delivery_configs(self, principal, account_id):
        return list(self.configs.get(account_id, ()))


class Clock:
    def __init__(self, start: str) -> None:
        self.now = at(start)

    def __call__(self) -> datetime:
        return self.now

    def set(self, text: str) -> None:
        self.now = at(text)


class World:
    """A seller (principal service + engine) and the buyer's own records."""

    def __init__(
        self,
        *,
        start: str = "2026-10-01T00:00:00Z",
        support: ReportingSubscriptionSupport | None = None,
        retention_days: int = 400,
    ) -> None:
        self.clock = Clock(start)
        self.host = Host()
        self.changes: list[PrincipalChange] = []

        async def emit(change: PrincipalChange) -> None:
            self.changes.append(change)

        self.principals = PrincipalService(emit_change=emit)
        self.engine = ReportingSubscriptionEngine(
            principals=self.principals,
            host=self.host,
            offerings=OFFERINGS,
            support=support,
            status_retention_days=retention_days,
            clock=self.clock,
        )
        self.principals.set_reporting_subscriptions(self.engine.principal_options())
        self.retention_days = retention_days
        self.sent: dict[str, dict[int, dict]] = {}
        self.grants: dict[str, list[SubscriptionAuthorization]] = {}
        self._key = 0

    async def sync(self, *subscriptions: dict) -> None:
        self._key += 1
        response = await self.principals.sync_principal(
            IDENTITY,
            {
                "idempotency_key": f"engine-test-key-{self._key:06d}",
                "configuration": {"reporting_subscriptions": list(subscriptions)},
            },
        )
        assert response.result.kind == "applied", response
        for item in subscriptions:
            self.sent.setdefault(item["subscription_id"], {})[item["subscription_version"]] = item

    def grant(self, account_id: str, when: str, **kwargs) -> None:
        self.host.accounts[account_id] = AuthorizedAccount(
            account_id, timezone=kwargs.pop("timezone", NY), authorized_at=at(when), **kwargs
        )
        history = self.grants.setdefault(account_id, [])
        history.append(SubscriptionAuthorization(at(when)))

    def revoke(self, account_id: str, when: str) -> None:
        del self.host.accounts[account_id]
        history = self.grants[account_id]
        history[-1] = SubscriptionAuthorization(history[-1].authorized_at, at(when))

    async def reconcile(self, when: str) -> None:
        self.clock.set(when)
        await self.engine.reconcile(IDENTITY)

    async def state(self, subscription_id: str):
        current = await self.principals.get_principal(IDENTITY)
        return next(
            item
            for item in current.result.configuration.reporting_subscriptions
            if item.subscription_id == subscription_id
        )

    async def coverage(self, account_id: str, subscription_id: str):
        entries = await self.engine.subscription_coverage(IDENTITY, account_id)
        return next((item for item in entries if item.subscription_id == subscription_id), None)

    async def seller_owes(self, subscription_id: str, account_id: str, now: str):
        return await self.engine.owed_periods(IDENTITY, subscription_id, account_id, now=at(now))

    async def buyer_expects(self, subscription_id: str, account_id: str, now: str, coverage=None):
        """What the buyer computes from what it can see, never from engine internals."""
        state = await self.state(subscription_id)
        excluded = coverage.excluded if coverage is not None else ()
        gaps = (coverage.gaps or ()) if coverage is not None else ()
        return expected_subscription_periods(
            subscription=state,
            version_configurations=self.sent[subscription_id],
            account_id=account_id,
            buys=self.host.buys.get(account_id, ()),
            offerings=OFFERINGS,
            account_timezone=NY,
            now=at(now),
            authorizations=self.grants.get(account_id, ()),
            exceptions=excluded,
            gaps=gaps,
            backfill_on_regrant=self.engine.support.backfill_on_regrant,
            status_retention_days=self.retention_days,
        )

    async def assert_conformant(
        self, subscription_id: str, account_id: str, now: str, coverage=None
    ) -> list:
        seller = await self.seller_owes(subscription_id, account_id, now)
        buyer = await self.buyer_expects(subscription_id, account_id, now, coverage)
        assert buyer == seller
        return seller


def table(periods) -> list[tuple]:
    return [
        (p.period_start, p.offering_id, p.delivery_config_version, p.media_buy_ids) for p in periods
    ]


# -- conformance cases -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_period_confirmation_cancellation_and_completion() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [
        buy(
            "mb-confirmed-late",
            "2026-10-02T04:00:00Z",
            "2026-10-08T04:00:00Z",
            confirmed="2026-10-03T15:00:00Z",
        ),
        buy(
            "mb-canceled",
            "2026-10-02T04:00:00Z",
            "2026-10-08T04:00:00Z",
            canceled_at=at("2026-10-04T16:00:00Z"),
        ),
        buy(
            "mb-completed",
            "2026-10-02T04:00:00Z",
            "2026-10-08T04:00:00Z",
            completed_at=at("2026-10-03T01:00:00Z"),
        ),
    ]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-09T00:00:00Z")
    assert table(owed) == [
        ("2026-10-02T04:00:00Z", "pkg", 1, ("mb-canceled", "mb-completed")),
        ("2026-10-03T04:00:00Z", "pkg", 1, ("mb-canceled",)),
        ("2026-10-04T04:00:00Z", "pkg", 1, ("mb-canceled", "mb-confirmed-late")),
        ("2026-10-05T04:00:00Z", "pkg", 1, ("mb-confirmed-late",)),
        ("2026-10-06T04:00:00Z", "pkg", 1, ("mb-confirmed-late",)),
        ("2026-10-07T04:00:00Z", "pkg", 1, ("mb-confirmed-late",)),
    ]


@pytest.mark.asyncio
async def test_flight_extension() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [
        buy(
            "mb-1",
            "2026-10-02T04:00:00Z",
            "2026-10-09T04:00:00Z",
            superseded_flights=(
                SubscriptionBuyFlight(
                    at("2026-10-06T12:00:00Z"),
                    at("2026-10-02T04:00:00Z"),
                    at("2026-10-04T04:00:00Z"),
                ),
            ),
        ),
    ]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-10T00:00:00Z")
    assert [p.period_start for p in owed] == [
        "2026-10-02T04:00:00Z",
        "2026-10-03T04:00:00Z",
        "2026-10-06T04:00:00Z",
        "2026-10-07T04:00:00Z",
        "2026-10-08T04:00:00Z",
    ]


@pytest.mark.asyncio
async def test_new_subscription_version() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-06T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    world.clock.set("2026-10-03T09:30:00Z")
    v2 = subscription(
        subscription_version=2,
        selector={
            "feed_purpose": "analytics",
            "period_duration": "PT1H",
            "required_finality": "official",
            "dimensions": ["package"],
        },
    )
    await world.sync(v2)
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-04T07:00:00Z")
    daily = [(p.period_start, p.delivery_config_version) for p in owed if p.offering_id == "pkg"]
    hourly = [p.period_start for p in owed if p.offering_id == "pkg-hourly"]
    assert daily == [
        ("2026-10-01T04:00:00Z", 1),
        ("2026-10-02T04:00:00Z", 1),
        ("2026-10-03T04:00:00Z", 1),
    ]
    # v2 starts at E, the end of the Oct 3 local day.
    assert hourly[0] == "2026-10-04T04:00:00Z"
    assert len(hourly) == 3


@pytest.mark.asyncio
async def test_revocation_and_regrant_without_backfill() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-12T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")

    world.revoke("acct-1", "2026-10-04T08:00:00Z")
    await world.reconcile("2026-10-04T08:00:00Z")
    state = await world.state("analytics")
    entry = state.accounts_unauthorized[0]
    assert entry.account_id == "acct-1"
    assert entry.unauthorized_at == at("2026-10-04T08:00:00Z")
    assert entry.last_owed_period_end == at("2026-10-04T04:00:00Z")
    assert entry.withheld_period_count == 1  # the Oct 3 day was not yet due
    assert await world.engine.subscription_coverage(IDENTITY, "acct-1") == []
    assert any(c.reason == "reporting_subscription_changed" for c in world.changes)
    await world.assert_conformant("analytics", "acct-1", "2026-10-05T00:00:00Z")

    world.grant("acct-1", "2026-10-06T15:00:00Z")
    await world.reconcile("2026-10-06T15:00:00Z")
    assert (await world.state("analytics")).accounts_unauthorized is None
    coverage = await world.coverage("acct-1", "analytics")
    assert [(g.from_, g.until) for g in coverage.gaps] == [
        (at("2026-10-04T04:00:00Z"), at("2026-10-07T04:00:00Z"))
    ]
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-10T00:00:00Z", coverage)
    assert [(p.period_start, p.expected_at) for p in owed] == [
        ("2026-10-01T04:00:00Z", "2026-10-02T10:00:00Z"),
        ("2026-10-02T04:00:00Z", "2026-10-03T10:00:00Z"),
        ("2026-10-03T04:00:00Z", "2026-10-06T21:00:00Z"),
        ("2026-10-07T04:00:00Z", "2026-10-08T10:00:00Z"),
        ("2026-10-08T04:00:00Z", "2026-10-09T10:00:00Z"),
    ]


@pytest.mark.asyncio
async def test_withheld_count_uses_the_delivery_hook_when_present() -> None:
    world = World()
    delivered: set[str] = {"2026-10-01T04:00:00Z"}

    async def is_period_delivered(principal, account_id, period, *, by):
        assert by == at("2026-10-04T08:00:00Z")
        return period.period_start in delivered

    world.host.is_period_delivered = is_period_delivered  # type: ignore[attr-defined]
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-12T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    world.revoke("acct-1", "2026-10-04T08:00:00Z")
    await world.reconcile("2026-10-04T08:00:00Z")
    # Oct 2 and Oct 3 ended before the loss and were not delivered.
    entry = (await world.state("analytics")).accounts_unauthorized[0]
    assert entry.withheld_period_count == 2


@pytest.mark.asyncio
async def test_revocation_and_regrant_with_backfill() -> None:
    world = World(support=ReportingSubscriptionSupport(backfill_on_regrant=True))
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-12T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    world.revoke("acct-1", "2026-10-04T08:00:00Z")
    await world.reconcile("2026-10-04T08:00:00Z")
    world.grant("acct-1", "2026-10-06T15:00:00Z")
    await world.reconcile("2026-10-06T15:00:00Z")
    coverage = await world.coverage("acct-1", "analytics")
    assert coverage.gaps is None
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-09T00:00:00Z", coverage)
    assert [p.period_start for p in owed] == [f"2026-10-{day:02d}T04:00:00Z" for day in range(1, 8)]


@pytest.mark.asyncio
async def test_max_accounts_is_deterministic_and_reported() -> None:
    world = World(support=ReportingSubscriptionSupport(max_accounts=2))
    world.grant("acct-b", "2026-09-01T00:00:00Z")
    world.grant("acct-a", "2026-09-01T00:00:00Z")
    world.grant("acct-c", "2026-09-02T00:00:00Z")
    for account in ("acct-a", "acct-b", "acct-c"):
        world.host.buys[account] = [
            buy(f"mb-{account}", "2026-10-01T04:00:00Z", "2026-10-05T04:00:00Z")
        ]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    state = await world.state("analytics")
    assert [(e.account_id, e.reasons) for e in state.accounts_with_exceptions] == [
        ("acct-c", ["capacity"])
    ]
    coverage = await world.coverage("acct-c", "analytics")
    assert coverage.status == "excluded"
    assert coverage.excluded[0].reason == "capacity"
    assert (
        await world.assert_conformant("analytics", "acct-c", "2026-10-06T00:00:00Z", coverage) == []
    )
    # acct-a leaves: acct-c is served from the next boundary.
    world.revoke("acct-a", "2026-10-02T13:00:00Z")
    await world.reconcile("2026-10-02T13:00:00Z")
    coverage = await world.coverage("acct-c", "analytics")
    assert coverage.status == "active"
    assert coverage.excluded[0].until == at("2026-10-03T04:00:00Z")
    owed = await world.assert_conformant("analytics", "acct-c", "2026-10-06T00:00:00Z", coverage)
    assert [p.period_start for p in owed] == ["2026-10-03T04:00:00Z", "2026-10-04T04:00:00Z"]


@pytest.mark.asyncio
async def test_max_accounts_slots_are_kept_by_served_accounts() -> None:
    # acct-a lost its slot when it lost authority; acct-b, served since,
    # keeps it when acct-a comes back.
    world = World(support=ReportingSubscriptionSupport(max_accounts=1), retention_days=2)
    world.grant("acct-a", "2026-09-01T00:00:00Z")
    await world.sync(subscription())
    await world.reconcile("2026-09-01T00:00:00Z")
    world.revoke("acct-a", "2026-09-02T00:00:00Z")
    await world.reconcile("2026-09-02T00:00:00Z")
    world.grant("acct-b", "2026-09-05T00:00:00Z")
    await world.reconcile("2026-09-05T00:00:00Z")
    world.grant("acct-a", "2026-09-10T00:00:00Z")
    await world.reconcile("2026-09-10T00:00:00Z")
    state = await world.state("analytics")
    assert [(e.account_id, e.reasons) for e in state.accounts_with_exceptions] == [
        ("acct-a", ["capacity"])
    ]
    # A byte-lower account granted later does not displace the served one.
    world.grant("acct-0", "2026-09-11T00:00:00Z")
    await world.reconcile("2026-09-11T00:00:00Z")
    state = await world.state("analytics")
    assert [(e.account_id, e.reasons) for e in state.accounts_with_exceptions] == [
        ("acct-0", ["capacity"]),
        ("acct-a", ["capacity"]),
    ]


@pytest.mark.asyncio
async def test_precedence_over_an_account_level_configuration() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-08T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    world.host.configs["acct-1"] = [AccountDeliveryConfig("acme-analytics", "analytics")]
    await world.reconcile("2026-10-02T10:00:00Z")
    coverage = await world.coverage("acct-1", "analytics")
    assert coverage.status == "excluded"
    entry = coverage.excluded[0]
    assert entry.reason == "overridden"
    assert entry.overridden_by.delivery_config_id == "acme-analytics"
    assert entry.from_ == at("2026-10-03T04:00:00Z")
    world.host.configs["acct-1"] = []
    await world.reconcile("2026-10-04T20:00:00Z")
    coverage = await world.coverage("acct-1", "analytics")
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-09T00:00:00Z", coverage)
    assert [p.period_start for p in owed] == [
        "2026-10-01T04:00:00Z",
        "2026-10-02T04:00:00Z",
        "2026-10-05T04:00:00Z",
        "2026-10-06T04:00:00Z",
        "2026-10-07T04:00:00Z",
    ]


@pytest.mark.asyncio
async def test_account_ids_subscription_beats_all_accounts() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.grant("acct-2", "2026-09-01T00:00:00Z")
    for account in ("acct-1", "acct-2"):
        world.host.buys[account] = [
            buy(f"mb-{account}", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")
        ]
    await world.sync(subscription("all"), subscription("named", scope={"account_ids": ["acct-2"]}))
    await world.reconcile("2026-10-01T00:00:00Z")
    coverage = await world.coverage("acct-2", "all")
    assert coverage.excluded[0].reason == "overridden"
    assert coverage.excluded[0].overridden_by.subscription_id == "named"
    assert await world.assert_conformant("all", "acct-2", "2026-10-04T00:00:00Z", coverage) == []
    named = await world.assert_conformant(
        "named", "acct-2", "2026-10-04T00:00:00Z", await world.coverage("acct-2", "named")
    )
    assert len(named) == 2
    assert len(await world.assert_conformant("all", "acct-1", "2026-10-04T00:00:00Z")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "match, expected",
    [
        ("exact", ["pkg"]),
        ("finest", ["pkg-country"]),
        ("all", ["pkg", "pkg-country", "pkg-creative"]),
    ],
)
async def test_exact_finest_and_all(match: str, expected: list[str]) -> None:
    # pkg-country sorts before pkg-creative in byte order, so finest picks it.
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z")]
    await world.sync(
        subscription(
            selector={
                "feed_purpose": "analytics",
                "period_duration": "P1D",
                "required_finality": "official",
                "dimensions": ["package"],
                "match": match,
            }
        )
    )
    await world.reconcile("2026-10-01T00:00:00Z")
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-03T00:00:00Z")
    assert [p.offering_id for p in owed] == expected


@pytest.mark.asyncio
async def test_unsupported_buys_and_packages_and_buyer_exclusions() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.grant("acct-x", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [
        buy(
            "mb-none",
            "2026-10-01T04:00:00Z",
            "2026-10-03T04:00:00Z",
            offering_ids=("pkg-creative",),
        ),
        SubscriptionBuy(
            media_buy_id="mb-some",
            confirmed_at=at("2026-10-01T00:00:00Z"),
            start_time=at("2026-10-01T04:00:00Z"),
            end_time=at("2026-10-03T04:00:00Z"),
            packages=(
                SubscriptionBuyPackage("p1", ("pkg",)),
                SubscriptionBuyPackage("p2", ("pkg-creative",)),
            ),
        ),
    ]
    await world.sync(subscription(exclude_account_ids=["acct-x"]))
    await world.reconcile("2026-10-01T00:00:00Z")
    coverage = await world.coverage("acct-1", "analytics")
    assert coverage.status == "active"
    assert [(e.media_buy_id, e.package_ids) for e in coverage.excluded] == [
        ("mb-none", None),
        ("mb-some", ["p2"]),
    ]
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-04T00:00:00Z", coverage)
    assert {p.media_buy_ids for p in owed} == {("mb-some",)}
    excluded = await world.coverage("acct-x", "analytics")
    assert excluded.status == "excluded"
    assert excluded.excluded[0].reason == "excluded_by_buyer"
    state = await world.state("analytics")
    assert [(e.account_id, e.reasons) for e in state.accounts_with_exceptions] == [
        ("acct-1", ["offering_unsupported"]),
        ("acct-x", ["excluded_by_buyer"]),
    ]


@pytest.mark.asyncio
async def test_expired_unauthorized_entry_drops_and_regrant_is_a_fresh_start() -> None:
    world = World(retention_days=2)
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-12T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    world.revoke("acct-1", "2026-10-03T08:00:00Z")
    await world.reconcile("2026-10-03T08:00:00Z")
    assert (await world.state("analytics")).accounts_unauthorized
    await world.reconcile("2026-10-06T00:00:00Z")
    assert (await world.state("analytics")).accounts_unauthorized is None
    world.grant("acct-1", "2026-10-07T12:00:00Z")
    await world.reconcile("2026-10-07T12:00:00Z")
    coverage = await world.coverage("acct-1", "analytics")
    assert coverage.gaps is None
    owed = await world.assert_conformant("analytics", "acct-1", "2026-10-10T00:00:00Z", coverage)
    assert [(p.period_start, p.withheld) for p in owed] == [
        ("2026-10-01T04:00:00Z", False),
        ("2026-10-02T04:00:00Z", True),
        ("2026-10-08T04:00:00Z", False),
    ]


@pytest.mark.asyncio
async def test_reactivation_in_place_is_a_fresh_start() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    world.host.buys["acct-1"] = [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-09T04:00:00Z")]
    await world.sync(subscription())
    await world.reconcile("2026-10-01T00:00:00Z")
    world.clock.set("2026-10-03T12:00:00Z")
    await world.sync(subscription(active=False))
    world.clock.set("2026-10-05T12:00:00Z")
    await world.sync(subscription())
    state = await world.state("analytics")
    assert state.state == "active" and state.deactivated_at is None
    owed = await world.seller_owes("analytics", "acct-1", "2026-10-09T05:00:00Z")
    # Oct 3 started before deactivation; the next owed day starts at the
    # first boundary after reactivation.
    assert [p.period_start for p in owed] == [
        "2026-10-01T04:00:00Z",
        "2026-10-02T04:00:00Z",
        "2026-10-03T04:00:00Z",
        "2026-10-06T04:00:00Z",
        "2026-10-07T04:00:00Z",
        "2026-10-08T04:00:00Z",
    ]


@pytest.mark.asyncio
async def test_receipt_roster_follows_active_receipt_subscriptions() -> None:
    world = World()
    world.grant("acct-1", "2026-09-01T00:00:00Z")
    await world.principals.sync_principal(
        IDENTITY,
        {
            "idempotency_key": "engine-roster-key-0001",
            "configuration": {
                "reporting_destinations": [
                    {
                        "pattern": "file_transfer",
                        "destination_id": "archive",
                        "active": True,
                        "provider": {"domain": "object-store.example"},
                        "transport": "s3",
                        "location": "s3://buyer-reporting/adcp/",
                        "accepted_formats": ["parquet"],
                        "accepted_verification_profiles": ["manifest_checksums"],
                    }
                ],
                "reporting_subscriptions": [
                    subscription(
                        "billing",
                        selector={
                            "feed_purpose": "billing",
                            "period_duration": "P1D",
                            "required_finality": "official",
                            "dimensions": ["package"],
                        },
                        reconciliation_mode="consumer_receipt",
                        destination={
                            "destination_id": "archive",
                            "path_template": "billing/{account_id}",
                        },
                    ),
                    subscription(),
                ],
            },
        },
    )
    await world.reconcile("2026-10-01T00:00:00Z")
    assert await world.engine.receipt_roster(IDENTITY, "acct-1") == ["billing"]
    assert await world.engine.receipt_roster(IDENTITY, "acct-unknown") == []
