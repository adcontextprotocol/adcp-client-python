# Subscribing to reports (buyer)

`client.reporting` registers AdCP 3.3 reporting subscriptions (experimental) safely. Every write is patch-shaped over the replace-shaped wire: other subscriptions are kept byte for byte, versions are chosen for you, and nothing is deactivated by omission.

## Subscribe

```python
result = await client.reporting.subscribe(
    {
        "subscription_id": "analytics-daily",
        "scope": {"all_authorized_accounts": True},
        "operator_domains": ["wppmedia.fr"],
        "selector": {"feed_purpose": "analytics", "period_duration": "P1D",
                     "required_finality": "official", "dimensions": ["package"]},
        "reconciliation_mode": "delivery_only",
        "destination": {"destination_id": "reporting-archive",
                        "path_template": "analytics/{account_id}/{period_start}"},
    },
    register_destination=archive_destination,
)
result.action, result.subscription_version
```

- **Versioning.** The version is bumped only when the selector, scope, destination, `reconciliation_mode`, `exclude_account_ids` or `operator_domains` change.
- **Pre-dispatch checks.** These fail with `dispatched=False`:
  - the scope must be explicit;
  - the destination must exist;
  - a multi-account path needs `{account_id}`;
  - the id must not clash with a `delivery_config_id`;
  - the operator scope must be safe;
  - the selector must match an offering.
- **Conflicts.** A conflicting write is re-read and retried.

## Unsubscribe

`await client.reporting.unsubscribe("analytics-daily")` is the only way to deactivate. Subscribing again reactivates the subscription at the same version.

## Why replace needs a flag

`replace_subscriptions(list, deactivate_omitted=True)` is the expert complete-set write. Sending `[]` needs `deactivate_all=True`. A raw `client.sync_principal(...)` carrying `reporting_subscriptions` or `reporting_destinations` is checked before dispatch. It is refused with `SUBSCRIPTION_WOULD_DEACTIVATE` or `DESTINATION_WOULD_REVOKE` unless you pass `allow_deactivation=True` or `allow_revocation=True`.

## Operator domains for agencies

An `all_authorized_accounts` subscription follows every grant, including accounts another operator grants later. When the principal spans operators, `subscribe` refuses an all-accounts scope without `operator_domains` (`SUBSCRIPTION_OPERATOR_SCOPE_REQUIRED`). Pass `acknowledge_cross_operator=True` to proceed anyway.

## Destinations

`register_destination=` upserts the named destination in the same write. Omitting a destination from a raw `sync_principal` revokes it for every account.

## Reconcile

`client.reporting.reconcile()` and `explain()` arrive in a later beta. Until then, `expected_subscription_periods(...)` and `ReportingSubscriptionTracker` compute owed periods from your own records (see [Coverage, explained](reporting-subscriptions-coverage.md#the-owed-rules)).

## Events

`client.reporting.poll()`, `events()` and `handle_webhook()` arrive in a later beta. Until then, pass each `principal.changed` payload to `tracker.handle_notification(...)`.

## Receipts for billing feeds

The receipts policy arrives in a later beta. Submit receipts with `sync_reporting_receipts` after your warehouse commits the data.
