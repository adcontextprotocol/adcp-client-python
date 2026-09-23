# Account-qualified reporting generations

The fix for [#1169](https://github.com/adcontextprotocol/adcp-client-python/issues/1169)
changes a reporting configuration generation's identity to
`(account_id, delivery_config_id, delivery_config_version)`. Two accounts can
each accept `daily@1`, with different immutable contents, in one ledger.

Use the frozen, hashable public value for maps and joins:

```python
from adcp.reporting.ledger import ReportingConfigurationGenerationKey

key = ReportingConfigurationGenerationKey(
    account_id="account-a",
    delivery_config_id="daily",
    delivery_config_version=1,
)
generations[key] = configuration
```

`ReportingConfiguration.generation_key` now returns this value. Code that
unpacked or indexed the beta.15 two-tuple must use the named attributes instead.
`ReportingObligationRecord`, `ConsumerStatusRecord`, and `LeasedConfiguration`
also expose `generation_key`. The existing constructors and store method
arguments, including `find_obligation` and the lease/release calls, remain
compatible. Consumer-status chain tuples, retained hashes, and derived
obligation/issue identifiers keep their existing serialization.

The worker lease API still selects work across the store's accounts. Resolve
work using the returned lease's account and generation. Releases match its
account, config ID, version, worker ID, and expiry; a release of an expired
handle cannot clear a newer lease held under the same worker ID. Callers must
retain the returned expiry when persisting or reconstructing a lease handle.
Passing a configuration from a different account or generation to
`ReportingProducer.acquire_obligation` now raises
`CONFIGURATION_GENERATION_MISMATCH` before touching the source.

Seller-issued obligation IDs remain globally unique. A low-level write that
reuses an ID for a different logical period now raises
`OBLIGATION_IDENTITY_CONFLICT` in both stores; it cannot overwrite another
account's obligation in memory. When filtering consumer statements by
obligation IDs, the named obligations must exist in the requested account.

## Upgrading PostgreSQL from 8.0.0-beta.15

1. Stop and drain all older reporting workers and configuration writers that
   use this ledger. Keep them stopped throughout the upgrade. Older code
   still selects and releases generations without an account predicate, so
   mixing old and new workers is unsafe once accounts reuse a config ID.
2. With the upgraded SDK, run `await store.create_schema()` before starting
   reporting work. It creates missing tables and applies the bundled
   `reporting_ledger_account_generations.sql` migration in one transaction.
3. Restart reporting work with the upgraded SDK on every instance.

For deployments managed by a migration tool, the standalone migration is
[`reporting_ledger_account_generations.sql`](../src/adcp/reporting/ledger/reporting_ledger_account_generations.sql).
It upgrades an existing beta.15 ledger by itself, including in autocommit mode.
For a combined bootstrap and upgrade, run both bundled files in one transaction:

```sh
psql "$REPORTING_DATABASE_URL" --set=ON_ERROR_STOP=1 --single-transaction \
  -f src/adcp/reporting/ledger/reporting_ledger.sql \
  -f src/adcp/reporting/ledger/reporting_ledger_account_generations.sql
```

Use the ledger's existing `search_path` and a role that owns its tables. Both
SQL files are also included as resources in the installed SDK's
`adcp.reporting.ledger` package. Running only `CREATE TABLE IF NOT EXISTS`
leaves the old primary key in place and does not perform this upgrade.

The migration inspects the primary-key columns under an exclusive table lock
and replaces the beta.15 global primary key with the account-qualified key.
It preserves the constraint's name, including a renamed beta.15 key. Existing
configuration rows, leases, obligations, revisions and frozen rows,
adjustments, consumer statements, issue lifecycle records, change-feed
sequences, hashes, and unrelated constraints/indexes remain intact.

Bootstrap and migration share a transaction-scoped advisory lock, so concurrent
upgraded processes serialize their DDL. Repeated runs recognize the new primary
key and leave its index intact. A failed upgrade rolls back; the migration
does not use `CASCADE` or discard records. An unexpected primary key or an
adopter-added foreign key referencing the old key requires an explicit
adopter migration. The released beta.15 schema has no foreign keys referencing
`reporting_configurations`.

The key's index rebuild holds an `ACCESS EXCLUSIVE` lock on
`reporting_configurations`, temporarily blocking its reads, writes, and leases.
Allow a maintenance window appropriate to the number of retained generations;
large obligation/revision tables are not rewritten. Configure deployment
lock/statement timeouts to match that window and retry a rolled-back migration
after resolving the blocking condition. These lock semantics follow
[PostgreSQL's ALTER TABLE documentation](https://www.postgresql.org/docs/16/sql-altertable.html).

A rollback to beta.15 is unsafe once multiple accounts share a config ID and
version. Do not recreate the global key or delete conflicting account rows to
make an old worker start. Preserve the ledger and roll forward with a corrected
account-qualified implementation.

The in-memory store needs no schema migration. Restart it with the upgraded
SDK and reload accepted configurations from the adopter's source of truth.
