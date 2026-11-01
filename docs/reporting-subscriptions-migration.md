# Migrating from account-level `reporting_delivery_configs`

A subscription registers reporting once on the principal, so it follows the caller's grants across every account. Account-level `reporting_delivery_configs`, written through `sync_accounts`, register reporting once per account. The two coexist: during a migration, both can be live on the same account.

```python
# 1. Subscribe, scoped to the accounts already served at account level.
await client.reporting.subscribe({
    "subscription_id": "analytics-daily",            # never equal to a delivery_config_id
    "scope": {"account_ids": ["acc_a", "acc_b"]},
    "selector": {"feed_purpose": "analytics", "period_duration": "P1D", "required_finality": "official"},
    "reconciliation_mode": "delivery_only",
})
# 2. Check what the subscription would own: your account-level configs still take precedence.
report = await client.reporting.reconcile(include_not_owed=True)   # overridden periods read excluded:overridden
# 3. Hand over: end the account-level configs where the subscription resolves an offering.
await client.reporting.subscribe({**subscription, "replace_account_configs": True})
# 4. Widen the scope once every account is handed over.
await client.reporting.subscribe({**subscription, "scope": {"all_authorized_accounts": True},
                                  "operator_domains": ["agency.example"]})
```

## Precedence

A caller's own active `sync_accounts` `reporting_delivery_configs` entry for a `feed_purpose` takes precedence over its subscriptions on that account. The subscription records an `overridden` exception, and the two never owe the same period: `reconcile()` and `explain()` read those periods as `excluded:overridden`. On the managed path, supply `SubscriptionOptions.account_delivery_configs` so the engine sees those configurations.

## Overrides

Give each configuration its `active_from`, so the exception opens at that instant. A `subscription_id` never equals a `delivery_config_id` the caller uses: they share one namespace. `client.reporting.subscribe` refuses a clash before dispatch (`SUBSCRIPTION_ID_CLASH`). On the seller side, `reporting.principals.reporting_subscription_ids(...)` lets `sync_accounts` refuse the reverse clash.

## `replace_account_configs`

A subscription with `replace_account_configs: true` ends the caller's own account-level configurations with the same `feed_purpose` on in-scope accounts where it resolves an offering. It applies once per `subscription_version`, or when the flag changes to true; resending the same version does nothing more. Supply `SubscriptionOptions.end_account_configs(principal, account_id, ids, *, at)` together with `account_delivery_configs`; without them the flag is refused with `UNSUPPORTED_FEATURE`. Periods that started before the handover stay owed by the account-level configuration, and the subscription owes the rest: run `kit.assert_agreement` in the [test kit](reporting-subscriptions-testing.md#testing-today) across a handover before you ship it.
