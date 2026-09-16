# Reporting ledger migrations

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
   `reporting_ledger_account_generations.sql`,
   `reporting_ledger_obligation_currency.sql`,
   `reporting_ledger_reconciliation.sql`, and
   `reporting_notification_outbox.sql` migrations in one transaction.
3. Restart reporting work with the upgraded SDK on every instance.

For deployments managed by a migration tool, the standalone migration is
[`reporting_ledger_account_generations.sql`](../src/adcp/reporting/ledger/reporting_ledger_account_generations.sql).
It upgrades an existing beta.15 ledger by itself, including in autocommit mode.
For a combined bootstrap and upgrade, run all five bundled files in one transaction:

```sh
psql "$REPORTING_DATABASE_URL" --set=ON_ERROR_STOP=1 --single-transaction \
  -f src/adcp/reporting/ledger/reporting_ledger.sql \
  -f src/adcp/reporting/ledger/reporting_ledger_account_generations.sql \
  -f src/adcp/reporting/ledger/reporting_ledger_obligation_currency.sql \
  -f src/adcp/reporting/ledger/reporting_ledger_reconciliation.sql \
  -f src/adcp/reporting/ledger/reporting_notification_outbox.sql
```

Use the ledger's existing `search_path` and a role that owns its tables. All
SQL migrations are also included as resources in the installed SDK's
`adcp.reporting.ledger` package. Running only `CREATE TABLE IF NOT EXISTS`
leaves the old primary key in place and does not perform this upgrade.

The reconciliation migration adds empty optional evidence tables and nullable,
immutable managed digest/total evidence on revisions and adjustments. It does not infer historical
evidence or enable a delivery tier. See the [storage contract](reporting-reconciliation-storage.md)
for the records, migration invariants, and deferred writer/handler work.

The outbox migration adds empty event, delivery, and ordered status-dirty tables.
It preserves existing ledger rows and does not backfill historical events.
Notification enqueue remains disabled until the store is constructed with
`notifications=True`. Opted-in schema readiness checks the complete installed chain,
including constraints, indexes, and immutable-record guards. See the
[notification outbox contract](reporting-notification-outbox.md) for trusted
subscription wiring, retention, and the deferred status projector.

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

## Frozen currency: upgrading beta.15 or the #1169 schema

[#1171](https://github.com/adcontextprotocol/adcp-client-python/issues/1171)
adds `reporting_obligations.currency`. The migration runs after #1169 under the
same advisory lock and transaction. It is an idempotent, in-place nullable
column addition, with uppercase three-letter validation and a trigger that
rejects changes to a stored currency, including `NULL` to a guessed value.
The account-qualified keys and their migration remain intact.

**Legacy policy: preserve unknown, fail closed.** Neither beta.15 nor #1169
persisted the process currency on obligations. A definition URI/digest does not
contain its definition bytes; retained Core totals contain name/value pairs
without units. Row data may include a currency, but it is adapter-supplied
corroboration rather than proof of the originally accepted scope. Empty rows
and unfulfilled obligations carry even less evidence. Therefore the migration
leaves **every existing obligation's currency `NULL`**, including apparently
USD rows. It does not copy a current account setting, use a new resolver, read
buyer context, infer from an adapter row, or default historical obligations to USD.

The options considered are:

1. Backfill USD or today's account currency: rejected. Either can mislabel
   non-USD history, and even historically USD deployments require evidence
   beyond the retained ledger to prove that choice.
2. Recover from external, authenticated historical configuration/definition
   evidence: possible only through a separate, reviewed adopter migration.
   Corroborate the entire frozen account/media-buy/package scope, including
   every existing revision and adjustment; retain the provenance and audit
   trail. The SDK does not perform that repair or supply an unchecked backfill
   API. Its immutability trigger deliberately requires explicit operator work.
3. Retain `NULL`, prevent new monetary work and preserve readable history:
   **the implemented recommendation**. This makes no lossy assumption and is
   safe when historical evidence is unavailable.

Upgraded workers refuse acquisition, snapshot restatement, new revision and
new adjustment writes for unknown obligations with `CURRENCY_UNRESOLVED`. An
obligation that is already satisfied or officially closed has no acquisition
work to refuse, so it stays the no-op it was before the upgrade. Inside
`run_worker`, an unresolved obligation is reported in `WorkerTurn.slices_failed`
and escalated like any other stuck slice: every *other* period under the same
configuration still closes and publishes on that same turn. A direct
`acquire_obligation` call still raises, so an operator driving one period by
hand sees the failure.
Exact legacy obligation/revision/adjustment replays still return the retained
record without appending evidence. Status and content reads remain available;
unknown obligations project `HISTORY_UNAVAILABLE`, `action_required` and
`contact_seller`. Reads never resolve or backfill currency. A new accepted
generation can support **future** work with a proven currency; it must not be
used to relabel old periods or erase a missing historical obligation.

No existing rows, hashes, statuses, leases, revisions, adjustments or change-feed
sequences are rewritten. The new column has **no database default**. Existing
indexes/constraints are preserved; a currency check and immutability trigger
are added. The currency migration locks `reporting_obligations` exclusively
while adding/validating the column and check. Plan a maintenance window for
large tables; production-sized lock time has not been benchmarked. Unexpected
adopter column types/defaults fail and roll back rather than silently adapting.

Stop and drain **all** beta.15 and #1169-only reporting writers before upgrading;
they do not supply frozen currency. Run `create_schema()` or the bundled SQL
command above, then start upgraded writers. If #1169 is already installed, its
primary-key migration recognizes the account-qualified key without rebuilding
it. The standalone currency SQL also runs atomically on an installed #1169
schema, including with autocommit. Do not run older code against this schema:
it can ignore the new invariant or create unresolved obligations.

New low-level `ReportingObligationRecord` writes must explicitly include a
trusted `currency`. The optional Python field exists to deserialize legacy
history, not to authorize an unfrozen new publication. High-level users of
`ProducerOfferings(currency=...)` keep their fixed-currency convenience; new
obligations freeze that value. See [multi-account currency resolution and
monetary validation](reporting-currency.md) for the resolver and type examples.
