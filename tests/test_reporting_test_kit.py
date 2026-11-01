"""The reporting test kit: a seller in process, a virtual clock, buyers, and agreement."""

from __future__ import annotations

import pytest

from adcp.testing.reporting import AgreementError, offerings, reporting_test_kit

pytest_plugins = ["adcp.testing.reporting_pytest"]
pytestmark = pytest.mark.asyncio

SUBSCRIPTION = {
    "subscription_id": "analytics-daily",
    "scope": {"all_authorized_accounts": True},
    "selector": {
        "feed_purpose": "analytics",
        "period_duration": "P1D",
        "required_finality": "snapshot",
    },
    "reconciliation_mode": "delivery_only",
}
ACCOUNTS = [{"account_id": "acc_a", "timezone": "America/New_York"}]
BUYS = [
    {
        "media_buy_id": "mb_1",
        "account_id": "acc_a",
        "confirmed_at": "2026-11-03T15:00:00Z",
        "start_time": "2026-11-03T05:00:00Z",
        "end_time": "2026-11-30T05:00:00Z",
    }
]


def _kit(**options):
    return reporting_test_kit(
        accounts=ACCOUNTS,
        offerings=[offerings.daily_analytics(alignment="account_timezone")],
        buys=BUYS,
        **options,
    )


async def test_buyer_and_seller_agree_across_grants_cutovers_dst_and_cancellation() -> None:
    async with _kit() as kit:
        buyer = kit.buyer("agency-x")
        assert (await buyer.reporting.subscribe(SUBSCRIPTION)).action == "created"
        await kit.clock.advance("P5D")
        report = await buyer.reporting.reconcile(now=kit.clock.now())
        assert report.definitive, report.issues
        assert [(line.period["start"], line.status) for line in report.periods] == [
            ("2026-11-03T05:00:00Z", "delivered"),
            ("2026-11-04T05:00:00Z", "delivered"),
        ]
        await kit.assert_agreement("agency-x")
        await kit.grants.revoke("agency-x", "acc_a")
        await kit.clock.advance("P1D")
        regrant = await kit.grants.regrant("agency-x", "acc_a")
        assert regrant["chain"] == "continued" and "regranted_at" in regrant
        await kit.clock.advance("P2D")
        await kit.assert_agreement("agency-x")
        cutover = await kit.accounts.set_timezone(
            "acc_a", "America/Los_Angeles", principal="agency-x"
        )
        assert cutover["previous_timezone"] == "America/New_York" and cutover["cutovers"]
        await kit.clock.advance("P3D")
        await kit.assert_agreement("agency-x")
        await kit.buys.cancel("mb_1", principal="agency-x")
        await kit.clock.advance("P2D")
        await kit.assert_agreement("agency-x")


async def test_wind_down_keeps_coverage_and_reductions_refuse_other_tasks() -> None:
    async with _kit() as kit:
        buyer = kit.buyer("agency-x")
        await buyer.reporting.subscribe(SUBSCRIPTION)
        await kit.clock.advance("P4D")
        await kit.grants.wind_down("agency-x", "acc_a")
        await kit.clock.advance("P1D")
        report = await buyer.reporting.reconcile(now=kit.clock.now())
        assert {line.status for line in report.periods} <= {"delivered", "pending"}
        await kit.assert_agreement("agency-x")


async def test_a_dst_day_is_owed_as_a_25_hour_period() -> None:
    async with reporting_test_kit(
        start="2026-10-30T00:00:00Z",
        accounts=ACCOUNTS,
        offerings=[offerings.daily_analytics(alignment="account_timezone")],
        buys=[
            {
                **BUYS[0],
                "confirmed_at": "2026-10-30T00:00:00Z",
                "start_time": "2026-10-31T04:00:00Z",
            }
        ],
    ) as kit:
        buyer = kit.buyer("agency-x")
        await buyer.reporting.subscribe(SUBSCRIPTION)
        await kit.clock.advance_to("2026-11-03T00:00:00Z")
        report = await buyer.reporting.reconcile(now=kit.clock.now())
        periods = {line.period["start"]: line.period["end"] for line in report.periods}
        assert periods["2026-11-01T04:00:00Z"] == "2026-11-02T05:00:00Z"
        await kit.assert_agreement("agency-x")


async def test_a_seller_that_drops_an_owed_period_fails_agreement_with_traces(monkeypatch) -> None:
    async with _kit() as kit:
        buyer = kit.buyer("agency-x")
        await buyer.reporting.subscribe(SUBSCRIPTION)
        await kit.clock.advance("P5D")
        engine = kit.service._subscriptions.engine
        original = engine.owed_periods

        async def dropping(*args, **kwargs):
            return (await original(*args, **kwargs))[1:]

        monkeypatch.setattr(engine, "owed_periods", dropping)
        with pytest.raises(AgreementError) as error:
            await kit.assert_agreement("agency-x")
        assert "only the buyer owes" in str(error.value)
        assert "2026-11-03T05:00:00Z" in str(error.value)


async def test_a_custom_platform_needs_its_service_and_controller() -> None:
    with pytest.raises(ValueError, match="controller"):
        async with reporting_test_kit(platform=object()):
            pass


@pytest.mark.adcp_reporting_kit(accounts=ACCOUNTS, buys=BUYS)
async def test_the_pytest_fixture_runs_the_reference_seller(adcp_reporting_kit) -> None:
    buyer = adcp_reporting_kit.buyer("agency-x")
    await buyer.reporting.subscribe(SUBSCRIPTION)
    await adcp_reporting_kit.clock.advance("P3D")
    await adcp_reporting_kit.assert_agreement("agency-x")
