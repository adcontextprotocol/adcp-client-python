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

## Reconcile issues

`client.reporting.reconcile()` and `poll()` never return a smaller success. When a read fails, `report.definitive` is `False` and `report.issues` carries one `ReconcileIssue(code, message, account_id, subscription_id)` per problem. The message ends with what to retry. These are values, not exceptions.

| Code | Cause | Fix |
|---|---|---|
| `SUBSCRIPTION_PRINCIPAL_UNAVAILABLE` | `get_principal` failed, so nothing could be computed | retry once the seller answers (`explain()` raises `ReportingSubscriptionError` instead) |
| `SUBSCRIPTION_OFFERINGS_UNAVAILABLE` | capabilities could not be read | retry; no offering can resolve until it succeeds |
| `SUBSCRIPTION_ROSTER_UNAVAILABLE` | `list_accounts` failed or did not finish paging, or `accounts_unauthorized` lists more than its inline page | retry; a partial roster is never used |
| `SUBSCRIPTION_ACCOUNT_UNREADABLE` | `get_media_buys` or `get_reporting_status` failed for one account | retry; the account is reported from the last facts read |
| `SUBSCRIPTION_LEDGER_UNAVAILABLE` | the account's obligations walk did not complete | retry; the periods are listed without delivery status |
| `SUBSCRIPTION_ACCOUNT_NOT_COVERED` | an explicit `accounts=` id is not readable, or no subscription covers it | check `list_accounts` and the subscription scopes |
| `COVERAGE_INPUT_INCOMPLETE`, `COVERAGE_INPUT_INVALID` | an owed set could not be computed, for example a missing account zone | the `detail` names the input |

## Events and receipts

| Code | Class | `dispatched` | `recovery` | Fix |
|---|---|---|---|---|
| `REPORTING_EVENTS_CONFLICT` | `ReportingEventConflictError` | absent | transient | another worker acknowledged this snapshot first; poll again |
| `RECEIPT_WRITE_FAILED` | `ReportingReconciliationError` | absent | transient | the seller did not record the receipts; reconcile again |

A revision the receipts policy could not verify is listed in `report.receipts.failed` as `(reporting_obligation_id, code)`. The code is the inspector's, for example `MANIFEST_DIGEST_MISMATCH`, or `INSPECTION_FAILED`. Nothing is receipted for it.

## Accounts

`client.accounts.change()` refuses locally, before anything is sent, with `AccountRequestError` (`dispatched=False`, `recovery="correctable"`):

| Code | Cause | Fix |
|---|---|---|
| `ACCOUNT_REVISION_REQUIRED` | a change to `billing`, `billing_entity` or `payment_terms` without `revision` | pass the revision the change was approved against, read from `list_accounts` |
| `ACCOUNT_CHANGE_INVALID` | no attribute, or one `change()` cannot change | change `brand`, `operator_unit`, `currency`, `timezone` or a money term |
| `ACCOUNT_REQUESTS_UNSUPPORTED` | the seller does not advertise `account.requests` | change the account through `sync_accounts` |

A seller rejection is a value: `AccountRejected`. Its `requires_new_account` is `True` for `ACCOUNT_CHANGE_REQUIRES_NEW_ACCOUNT`; resend with `allow_separate_account=True`. A `CONFLICT` on a money change is raised as `AccountRequestError`, because the human must approve the change against the new revision.

## Coverage traces

`explain()` and `explain_coverage()` raise `CoverageInputError` (`COVERAGE_INPUT_INVALID`, detail `invalid_input`) when `period` is not a date, an instant or `{start, end}`, or when `{start, end}` is not a period of the offering's schedule. They raise `KeyError` for an unknown `subscription_id`. A trace's `reason` is always a code from the spec's `reason-codes.json`; see [Coverage, explained](reporting-subscriptions-coverage.md#reason-codes).

