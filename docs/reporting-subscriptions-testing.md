# Testing with the reporting test kit

## Testing today

`reporting_test_kit(...)` runs a seller in process under a virtual clock and gives you real buyer clients bound to it. The SDK's reference seller is the default. Pass your own platform to test your wiring.

```python
from adcp.testing.reporting import offerings, reporting_test_kit

async with reporting_test_kit(
    start="2026-11-01T00:00:00-04:00",
    offerings=[offerings.daily_analytics(alignment="account_timezone")],
    accounts=[{"account_id": "acc_a", "timezone": "America/New_York"}],
    buys=[{"media_buy_id": "mb_1", "account_id": "acc_a", "confirmed_at": "2026-11-03T15:00:00Z",
           "start_time": "2026-11-03T05:00:00Z", "end_time": "2026-11-30T05:00:00Z"}],
) as kit:
    buyer = kit.buyer("agency-x")                       # a real client.reporting
    await buyer.reporting.subscribe(analytics_daily)
    await kit.clock.advance("P3D")                      # the worker runs at each boundary crossed
    await kit.grants.revoke("agency-x", "acc_a")
    await kit.clock.advance("P1D")
    await kit.grants.regrant("agency-x", "acc_a")
    await kit.grants.wind_down("agency-x", "acc_a")
    await kit.accounts.set_timezone("acc_a", "America/Los_Angeles", principal="agency-x")
    await kit.clock.advance_to("2027-03-14T12:00:00-04:00")   # DST
    await kit.buys.cancel("mb_1", principal="agency-x")
    await kit.assert_agreement("agency-x")
```

- **Clock.** `kit.clock.advance("P3D")` (days to seconds) and `advance_to(instant)` move the one virtual clock forward. At every period boundary crossed, the seller processes the move as production would: the boundary sweep, then planning.
- **Grants, accounts and buys.** `kit.grants` (`revoke`, `regrant`, `reduce`, `wind_down`), `kit.accounts.set_timezone` and `kit.buys` (`add`, `cancel`) go through the controller's `reporting_subscription_probe` and `seed_media_buy`. That is the same scenario a storyboard drives against a remote seller.
- **Agreement.** `await kit.assert_agreement(principal)` checks that the seller's owed set equals what the buyer's `reconcile()` computes over the wire. It also checks that the `authorized_at` served on `list_accounts` is the one coverage used. On a difference, the `AgreementError` names each disputed period, the first rule at which the two `CoverageTrace`s differ, and both sentences.
- **Your platform.** Pass `platform=` (your installed handler), `service=` (your `ReliableReportingService`) and `controller=` (a `TestControllerStore` serving `reporting_subscription_probe` and `seed_media_buy`). The kit then drives your seller through that controller.
- **Offerings.** `offerings.daily_analytics(...)` and `offerings.hourly_pacing(...)` build offering fixtures that publish `standard_dimensions`.

## The test kit

The pytest fixture `adcp_reporting_kit` yields a started kit and closes it after the test:

```python
# conftest.py
pytest_plugins = ["adcp.testing.reporting_pytest"]

# test_reporting.py
@pytest.mark.adcp_reporting_kit(accounts=[{"account_id": "acc_a", "timezone": "UTC"}])
async def test_owed(adcp_reporting_kit):
    buyer = adcp_reporting_kit.buyer("agency-x")
    await buyer.reporting.subscribe(analytics_daily)
    await adcp_reporting_kit.clock.advance("P3D")
    await adcp_reporting_kit.assert_agreement("agency-x")
```

The marker's keyword arguments are those of `reporting_test_kit`. Run the fixture under pytest-asyncio.

For a lower-level test, `ReliableReportingService(storage=memory_storage(), subscriptions=...)` runs in process under pytest. Drive it with a `DeterministicReportingClock` and `run_worker()`, and call `await reporting.drain_subscription_triggers()` before asserting on triggered reconciles. `tests/test_reporting_subscription_managed.py` is a worked example.

Python sellers run the TypeScript storyboard CLI against their URL:

```
npx adcp storyboard run https://seller.example/mcp --storyboards reporting_subscriptions --auth "$TOKEN"
```
