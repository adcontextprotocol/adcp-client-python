# Migrating from account-level `reporting_delivery_configs`

## Precedence

A caller's own active `sync_accounts` `reporting_delivery_configs` entry for a `feed_purpose` takes precedence over its subscriptions on that account. The subscription records an `overridden` exception, and the two never owe the same period. On the managed path, supply `SubscriptionOptions.account_delivery_configs` so the engine sees those configurations.

## Overrides

Give each configuration its `active_from`, so the exception opens at that instant. A `subscription_id` never equals a `delivery_config_id` the caller uses: they share one namespace. `reporting.principals.reporting_subscription_ids(...)` lets `sync_accounts` refuse a clash.

## `replace_account_configs`

A subscription with `replace_account_configs: true` ends the caller's own account-level configurations with the same `feed_purpose` on in-scope accounts where it resolves an offering. It applies once per `subscription_version`, or when the flag changes to true. Supply `SubscriptionOptions.end_account_configs(principal, account_id, ids, *, at)` together with `account_delivery_configs`; without them the flag is refused with `UNSUPPORTED_FEATURE`.
