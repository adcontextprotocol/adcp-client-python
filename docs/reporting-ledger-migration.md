# Reporting ledger migrations


The caller-ownership release requires a stopped-worker maintenance upgrade. Follow [the caller-ownership migration contract](reporting-caller-ownership-migration.md) before applying these earlier migrations. Mixed-version reporting workers are unsupported for this upgrade.

The fix for [#1291](https://github.com/adcontextprotocol/adcp-client-python/issues/1291)
changes a reporting configuration generation's identity to
`(account_id, consumer_id, delivery_config_id, delivery_config_version)`. Two callers can
each accept `daily@1`, with different immutable contents, in one ledger.

Use the frozen, hashable public value for maps and joins:

```python
from adcp.reporting.ledger import ReportingConfigurationGenerationKey

key = ReportingConfigurationGenerationKey(
    account_id="account-a",
    consumer_id="buyer-a",
    delivery_config_id="daily",
    delivery_config_version=1,
)
generations[key] = configuration
```

`ReportingConfiguration.generation_key` now returns this value. Code that
unpacked or indexed the beta.15 two-tuple must use the named attributes instead.
`ReportingObligationRecord`, `ConsumerStatusRecord`, and `LeasedConfiguration`
also expose `generation_key`. This release requires caller identity in constructors and lookup APIs. Old
constructors and account-only lookups are unsupported. Retained evidence stays
unchanged in the stopped-worker archive; new generation identifiers and canonical
bindings include caller ownership. Follow the maintenance contract above before
restarting any reporting process.

The worker lease API still selects work across the store's accounts. Resolve
work using the returned lease's account and generation. Releases match its
account, caller, config ID, version, worker ID, and expiry; a release of an expired
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

## Reporting-subscription generations: the widened key (AdCP 3.3)

Reporting subscriptions keep one generation per resolved offering of a
subscription version, so the generation key gains `offering_id`:
`(account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id)`.
`ReportingConfigurationGenerationKey.offering_id` is `None` for every
account-level generation, and their digests, work keys and persisted scopes are
unchanged.

In PostgreSQL this is a **stopped-worker maintenance upgrade**:

1. Stop every reporting worker. Workers of earlier releases refuse the widened
   schema; mixed-version workers are unsupported.
2. Run `create_schema()` (or your usual migration entry point) once with the new
   release. Under the schema lock it widens the key in place and losslessly:
   - `reporting_configurations` gains `offering_id` (`''` for existing rows),
     and its primary key includes it;
   - `reporting_obligations` gains `offering_id`, and its period unique index
     includes it;
   - `reporting_consumer_statuses` gains `offering_id`;
   - the SDK's own references (reconciliation records, materializer discovery,
     status scope checkpoints, production generations) are re-pointed at the
     widened key. Reconciliation records, materializer discovery and status
     scope checkpoints also hold subscription generations, for managed
     delivery. Production generations hold account-level generations only,
     enforced by a `CHECK`.
3. Restart the workers on the new release.

A schema created by a release that widened the key but did not yet serve
managed subscription delivery still carries an account-only `CHECK` on
reconciliation records, materializer discovery and status scope checkpoints,
and a four-column materializer discovery key. `create_schema()` drops those
checks and widens the discovery key in place, losslessly and idempotently.
Earlier workers refuse the upgraded schema, so stop them first here too.

A foreign key outside the SDK that references `reporting_configurations`
refuses the upgrade with a maintenance error, rather than being dropped.
Re-point it at the widened key, then run the upgrade. The upgrade is
idempotent; a fresh schema is created in the widened layout directly.

### Status checkpoints keyed by offering (migration `0006`)

Status checkpoints, and the status events that reference them, are keyed by
the generation's resolved offering too, so several offerings of one
subscription version for the same account and owner each keep their own
status: `reporting_status_scope_checkpoints` is keyed by `(account_id,
consumer_namespace, delivery_config_id, version, scope_kind,
obligation_namespace, offering_id)`. Account-level checkpoints keep
`offering_id` `''`, so their keys, status `cause_id` values and projection
history keys are unchanged.

This is also a **stopped-worker upgrade**: stop every reporting worker, run
`python -m adcp migrate` (migration `0006 reporting_status_checkpoint_offerings`)
or the status, projection or production store's `create_schema()` once with the
new release, then restart the workers. Under the schema lock it widens the
checkpoint primary key; the status event tables (`reporting_status_notification_events`,
and `reporting_projection_notification_events` where the projection is
installed) gain `offering_id`, filled from their checkpoint, and their
checkpoint reference and per-generation unique key include it. Constraints
keep their names. It changes nothing on a schema without status
notifications. A foreign key outside the SDK that references
`reporting_status_scope_checkpoints` refuses the upgrade instead of being
dropped.

## Historical account-generation upgrade

The following notes describe the pre-ownership SDK release and are retained for
operators auditing earlier upgrade steps. They are not the upgrade procedure for
this release: startup now refuses legacy storage, including beta.15. Use the
[maintenance archive and authoritative backfill](reporting-caller-ownership-migration.md).

### PostgreSQL from 8.0.0-beta.15

1. Stop and drain all older reporting workers and configuration writers that
   use this ledger. Keep them stopped throughout the upgrade. Older code
   still selects and releases generations without an account predicate, so
   mixing old and new workers is unsafe once accounts reuse a config ID.
2. With the upgraded SDK, run `await store.create_schema()` before starting
   reporting work. It creates missing tables and applies the bundled
   `reporting_ledger_account_generations.sql`,
   `reporting_ledger_obligation_currency.sql`,
   `reporting_ledger_reconciliation.sql`, and
   `reporting_notification_outbox.sql` and `reporting_webhook_activity.sql`
   migrations in one transaction. For the later A-to-B additive upgrade and
   activity activation barrier, see [durable reporting activity](reporting-webhook-activity.md).
3. Restart reporting work with the upgraded SDK on every instance.

The producer retry schedule is stored in the additive
`adcp_reporting_producer_retry_schedules` table created by `reporting_ledger.sql`.
Run `create_schema()` (or that bundled SQL file) before starting upgraded
producers so retries and source-wide cooldowns survive worker restarts.
Custom ledger stores must implement `RetryScheduleStore`, or provide a durable
`retry_store` when constructing `ReportingProducer`. The producer rejects a
store with no shared retry state to avoid silently retrying on every replica.

For deployments managed by a migration tool, the standalone migration is
[`reporting_ledger_account_generations.sql`](../packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/reporting_ledger_account_generations.sql).
It upgrades an existing beta.15 ledger by itself, including in autocommit mode.
For a combined bootstrap and upgrade, run all five bundled files in one transaction:

```sh
psql "$REPORTING_DATABASE_URL" --set=ON_ERROR_STOP=1 --single-transaction \
  -f packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/reporting_ledger.sql \
  -f packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/reporting_ledger_account_generations.sql \
  -f packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/reporting_ledger_obligation_currency.sql \
  -f packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/reporting_ledger_reconciliation.sql \
  -f packages/adcp-reporting-storage/src/adcp_reporting_storage/ledger/reporting_notification_outbox.sql
```

Use the ledger's existing `search_path` and a role that owns its tables. All
SQL migrations are also included as resources in the installed SDK's
`adcp.reporting.ledger` package. Running only `CREATE TABLE IF NOT EXISTS`
leaves the old primary key in place and does not perform this upgrade.

The reconciliation migration adds empty optional evidence tables and nullable,
immutable managed digest/total evidence on revisions and adjustments. It does not infer historical
evidence or enable a delivery tier. Re-running it also replaces the
`reporting_reconciliation_reference_immutable()` guard function in place, so
that a referenced configuration generation keeps its published content
immutable while still accepting the rc.3 lifecycle columns (`activated_at`,
`deactivated_at`, `automated_recovery_seconds`, `status_retention_days`). It
rewrites no rows. See the [storage contract](reporting-reconciliation-storage.md)
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

## Upgrading a ledger created by adcp-reporting-storage 0.1.0b4 or 0.2.0b0 (#1763)

adcp 10.0.0b4 widened the configuration generation key with `offering_id`
(AdCP 3.3 reporting subscriptions) and reads it from `reporting_configurations`,
`reporting_obligations` and `reporting_consumer_statuses`. The storage releases
before 0.2.0b1 do not create the column, so that pair fails every reporting
turn with `column "offering_id" does not exist`. adcp 10.0.0b7 and later
refuse that pair at startup instead (storage contract 1 versus 2; see
`packages/adcp-reporting-storage/README.md`). adcp 10.0.0b4 and 10.0.0b5
predate the check and still install beside 0.2.0b0, so that pair fails every
turn: install `adcp>=10.0.0b7` with `adcp-reporting-storage>=0.2.0b1`
(10.0.0b6 was not published). From 0.2.0b1, both schema entry points
upgrade an existing ledger in place:

- `PgReportingLedgerStore.create_schema()`, which `ReliableReportingService`
  runs at startup when built with `store=`;
- `python -m adcp migrate`, which `postgres_storage(...)` requires to have run.

The upgrade (migration `0002`) adds `offering_id TEXT NOT NULL DEFAULT ''` to
the widened tables, so every existing account-level generation, obligation and
status keeps its identity with `offering_id = ''`; rebuilds the generation
primary key and the obligation period index with the new column; and re-points
the managed-delivery foreign keys. No evidence row is rewritten or dropped. The
applied versions are recorded in `adcp_schema_migrations` (`create_schema()`
applies the ledger's own `0001`–`0003`; `python -m adcp migrate` also applies
the later ones, which only `postgres_storage` and the notification stores
need), re-running is a no-op,
and concurrent boots serialize on the reporting schema advisory lock inside
one transaction. It is a stopped-worker upgrade: stop every worker of the
earlier release first, because their configuration writes name the
four-column key.

### Deploying the upgrade

This applies whenever more than one process runs Reliable Reporting against
the ledger. That includes several replicas that each start
`ReliableReportingService`, and a separate worker deployment. A default
rolling update does **not** stop the earlier release first. The first new
process upgrades the ledger when it starts, while the old processes keep
running against it. From then on, an old process:

- still reads configurations, obligations, revisions and rows;
- cannot record a new generation (`put_configuration` fails with
  `InvalidColumnReference: there is no unique or exclusion constraint matching
  the ON CONFLICT specification`);
- cannot close the next period of an existing generation (`commit_obligation`
  fails with `LedgerConflictError: the ledger write violated an integrity rule`,
  because the period key it names was widened);
- with status notifications, cannot record status checkpoints, whose key
  migration `0006` widens;
- cannot start Reliable Reporting again if it restarts. `create_schema()`
  raises `Reporting caller ownership requires a stopped-worker maintenance
  migration`, with a hint to use `migrate_legacy_reporting`. **Ignore that
  hint.** `migrate_legacy_reporting` is the SDK 8 → 9 caller-ownership
  migration. It does not apply to this upgrade and would move reporting state
  into quarantine.

Nothing is corrupted: the new release reads every record the old one wrote.
But the old processes' reporting stops for the rest of the rollout.

**There is no rollback.** Once the ledger is upgraded, no earlier release can
start Reliable Reporting against it. To go back, restore the backup taken
before the upgrade; evidence written after the backup is lost.

To deploy:

1. Back up the database.
2. Stop every reporting worker of the earlier release. Two ways:
   - **Recreate.** Roll this one deployment out with every old pod stopped
     before any new pod starts (Kubernetes `strategy: {type: Recreate}`).
     Reporting, and the pods, are down for the length of the swap.
   - **Two phases.** First redeploy the *earlier* release with Reliable
     Reporting turned off (don't start `ReliableReportingService`), or scale
     a separate worker deployment to zero. Then roll out the new release
     normally. Only reporting pauses, not the rest of the service.
3. Upgrade the schema: run `python -m adcp migrate` once, or let the first new
   process do it at startup. Run `python -m adcp migrate` (or
   `ReportingStatusService.migrate()`) if you use `postgres_storage(...)` or
   status notifications. `create_schema()` alone applies only `0001`–`0003`.
4. Start the new release everywhere.

`tests/conformance/reporting/test_reporting_storage_published_upgrade_pg.py`
seeds ledgers with the actual published wheel pairs (`adcp==9.3.1` with
`adcp-reporting-storage==0.1.0b4`, `adcp==10.0.0b2` with `0.2.0b0`) and proves
the retained evidence reads back unchanged, a restart rebinds the stored
generations, and the upgraded catalog is identical to a fresh migration.

## Upgrading a ledger that adcp-reporting-storage 0.1.0b5 wrote

0.1.0b5 is the SDK 9 storage release that orders the account change feed by
`(xid, seq)`, accepts 255-character row binding IDs and credential refs, and
adds the opt-in orphaned-object collector. Its ledger upgrades to this release
through the same two entry points and the same numbered migrations as a
0.1.0b4 ledger (above): this release adds no migration for them. Their DDL is
in the files `create_schema()` re-applies on every start when the option is
on (`reporting_changes.sql` with `change_feed=True`,
`reporting_row_storage.sql` with `row_storage`), as on the SDK 9 line, and
`python -m adcp migrate` never creates those tables. A ledger created before
0.1.0b5 gains the account horizon's xid columns, the named feed-consumer and
row-binding constraints, the `(account_id, xid, seq)` index and the orphan
collector fence table the first time this release starts with those options;
see [host change feed and consumers](reporting-row-storage.md#host-change-feed-and-consumers)
for creating that index concurrently on a large changes table.

adcp-reporting-storage 0.2.0b0 to 0.2.0b2 predate the `(xid, seq)` account
feed. On a 0.1.0b5 ledger they refuse an account cursor that carries an
`xid`, refuse a row binding ID over 128 characters, and prune the change feed
without advancing the account horizon's xid columns. A cursor 0.1.0b5 handed
out could then pass the account horizon check and skip what 0.2.x pruned. So
the first time this release starts with `change_feed=True` on a 0.1.0b5 ledger
that a 0.2 release has already upgraded, it raises each account horizon to the
deployment horizon, or to just below the account's oldest retained change when
that is lower. Those cursors then fail closed with `CURSOR_EXPIRED`; a
consumer starting over still reads every retained change. It runs once (a
comment on `adcp_reporting_change_account_horizon` records it) and changes
nothing on a ledger upgraded straight from 0.1.0b5. As for the `(xid, seq)`
upgrade on SDK 9, stop every older host's change maintenance before this
release starts.
