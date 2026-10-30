# Error reference

Every SDK error carries `code` and `recovery` (`transient`, `correctable` or `terminal`). Its message ends with the fix.

**`dispatched`.** `dispatched=False` means no bytes of the task left the process, so the operation certainly had no effect on the seller. Check it with `is_pre_dispatch_error(error)`. The property is absent (`None`) when the call may have reached the seller, and on local computations and configuration checks.

| Code | Class | `dispatched` | `recovery` | Fix |
|---|---|---|---|---|
| `SUBSCRIPTION_WOULD_DEACTIVATE` | `ReportingSubscriptionError` | `False` | correctable | use `unsubscribe()`, or pass `allow_deactivation=True` |
| `DESTINATION_WOULD_REVOKE` | `ReportingSubscriptionError` | `False` | correctable | pass `allow_revocation=True` |
| `SUBSCRIPTION_ID_CLASH` | `ReportingSubscriptionError` | `False` | correctable | rename; the message names the clashing `delivery_config_id` |
| `SUBSCRIPTION_OPERATOR_SCOPE_REQUIRED` | `ReportingSubscriptionError` | `False` | correctable | add `operator_domains`, or pass `acknowledge_cross_operator=True` |
| `SUBSCRIPTION_SELECTOR_UNMATCHED` | `ReportingSubscriptionError` | `False` | correctable | adjust the selector; the message names the nearest offerings |
| `SUBSCRIPTION_CONFLICT_EXHAUSTED` | `ReportingSubscriptionError` | absent | transient | retry later |
| `SUBSCRIPTION_READBACK_MISMATCH` | `ReportingSubscriptionError` | absent | terminal | contact the seller; `readback` is attached |
| `COVERAGE_INPUT_INCOMPLETE`, `COVERAGE_INPUT_INVALID` | `CoverageInputError` | n/a | correctable | supply the named input |
| `COVERAGE_BUDGET_EXCEEDED` | `CoverageInputError` | n/a | correctable | narrow the input |
| `REPORTING_STORAGE_NOT_DURABLE` | `ReliableReportingConfigurationError` | n/a | correctable | `storage=postgres_storage(...)`, or `ADCP_ENV=development` |
| `REPORTING_SCHEMA_BEHIND` | `ReliableReportingConfigurationError` | n/a | correctable | run `python -m adcp migrate` |
| `SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS` | `ReliableReportingConfigurationError` | n/a | correctable | publish `standard_dimensions` on the named offering |
| `SUBSCRIPTION_ROSTER_UNAVAILABLE` | `ReliableReportingConfigurationError` | n/a | correctable | serve `list_accounts`, or supply `SubscriptionOptions.accounts` |

`AccountRequestError` is raised only when `client.accounts.ensure` could not be processed; it carries the wire codes. Account outcomes are values.
