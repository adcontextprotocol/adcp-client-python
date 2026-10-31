# Testing with the reporting test kit

## Testing today

`ReliableReportingService(storage=memory_storage(), subscriptions=...)` runs in process under pytest. Drive it with a `DeterministicReportingClock` and `run_worker()`, and call `await reporting.drain_subscription_triggers()` before asserting on triggered reconciles. `tests/test_reporting_subscription_managed.py` is a worked example.

## The test kit

`reporting_test_kit(...)` and the `adcp_reporting_kit` pytest fixture arrive in a later beta. They add a virtual clock, grants, accounts, buys, and `assert_agreement`. Python sellers run the TypeScript storyboard CLI against their URL:

```
npx adcp storyboard run https://seller.example/mcp --storyboards reporting_subscriptions --auth "$TOKEN"
```
