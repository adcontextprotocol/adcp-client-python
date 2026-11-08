# adcp-reporting-storage

PostgreSQL and object-storage implementations of the AdCP Reliable Reporting
stores, maintained in the [adcp-client-python](https://github.com/adcontextprotocol/adcp-client-python)
repository and versioned on its own line.

[`adcp`](https://pypi.org/project/adcp/) defines the store protocols, the
in-memory stores, the producer, the `get_reporting_status` handlers, the
reconciliation logic and the row encoding. This distribution implements those
protocols durably:

| What | Where |
| --- | --- |
| Core ledger, reconciliation, materializer, receipt, feed, projection and production stores over PostgreSQL | `adcp_reporting_storage.ledger.pg`, `.ledger.delivery_pg`, `.materializer.pg`, `.receipts.pg`, `.feed.pg`, `.projection.pg`, `.production.pg` |
| Notification, status and activity queues over PostgreSQL | `adcp_reporting_storage.outbox.pg`, `.outbox.status_pg`, `.outbox.status_activity_pg` |
| Chunked revision row storage, with filesystem, Google Cloud Storage, Amazon S3 and Azure Blob providers | `adcp_reporting_storage.ledger.row_storage*` |
| The host change feed, period retirement, row migration and retention | `adcp_reporting_storage.ledger.change_feed`, `.period_retirement`, `.row_migration`, `.row_retention` |
| Durable inline source staging and seals, buyer submission intents | `adcp_reporting_storage.inline_storage`, `.submissions.pg` |
| Caller-ownership migration helpers | `adcp_reporting_storage.migration` |
| The adapter-first `ReliableReportingService` over a borrowed pool | `adcp_reporting_storage.service` |

Every public name is also importable from the top-level package, lazily:

```python
from psycopg_pool import AsyncConnectionPool

from adcp.reporting.ledger import ProducerOfferings, ReportingProducer
from adcp_reporting_storage import PgReportingLedgerStore, ReportingRowStorageOptionsV1

async with AsyncConnectionPool(dsn, min_size=2, max_size=10) as pool:
    store = PgReportingLedgerStore(
        pool=pool,
        row_storage=ReportingRowStorageOptionsV1(deployment_namespace="prod-us"),
    )
    await store.create_schema()
    producer = ReportingProducer(source=..., offerings=ProducerOfferings(...), store=store)
    await producer.run_worker()
```

## Install

```bash
pip install "adcp-reporting-storage[pg]"            # PostgreSQL stores (psycopg)
pip install "adcp-reporting-storage[pg,gcs]"        # + rows on Google Cloud Storage
pip install "adcp-reporting-storage[pg,s3]"         # + rows on Amazon S3
pip install "adcp-reporting-storage[pg,azure]"      # + rows on Azure Blob Storage
```

`adcp` is a dependency and is installed with it. The cloud providers take a
client you build; the extras only pin the client libraries.

## Compatibility

This package implements the protocols of one `adcp` major and imports a few
private helpers of `adcp.reporting` to do so (`adcp.reporting.ledger._record_rows`,
`_delivery_state`, `status_snapshot`). The column lists those helpers read are
the **storage contract**, numbered on both sides
(`adcp_reporting_storage.STORAGE_CONTRACT`,
`adcp.reporting.ledger.storage_contract.REPORTING_STORAGE_CONTRACT`) and bumped
together whenever the shared layout changes. The dependency floor is the first
`adcp` release of the contract this package implements, so this release
cannot be installed beside an older `adcp`. Forcing it anyway fails at import.
From adcp 10.0.0b7, building a PostgreSQL store also refuses a package of another
contract at startup (`REPORTING_SCHEMA_BEHIND`), naming both versions and the
release to install.

That check is in `adcp`, so it cannot cover releases that predate it:
`adcp` 10.0.0b4 and 10.0.0b5 still resolve beside 0.2.0b0 and fail every
reporting turn with `column "offering_id" does not exist`
([#1763](https://github.com/adcontextprotocol/adcp-client-python/issues/1763)).
Install `adcp>=10.0.0b7` with this release.

| adcp-reporting-storage | storage contract | adcp |
|---|---|---|
| 0.1.0b4+ (SDK 9 line) | 1 | `>=9.0.0,<10` |
| 0.2.0b0 | 1 | `>=9.0.0,<11` (**do not pair with 10.0.0b4+**: 10.0.0b4 and b5 install beside it and fail, b7+ refuse it, [#1763](https://github.com/adcontextprotocol/adcp-client-python/issues/1763)) |
| 0.2.0b1+ | 2 | `>=10.0.0b4,<11` |

`adcp` never imports this package; a test in the main repository enforces that.

### Upgrading a ledger created by 0.1.0b4 or 0.2.0b0

Both `PgReportingLedgerStore.create_schema()` (what `ReliableReportingService`
runs at startup when given `store=`) and `python -m adcp migrate` upgrade an
existing ledger, and **a default rolling update works for the ledger, the
notification outbox and status notifications** (the materializer, production
and status projection extensions still need their earlier-release workers
stopped first, below). The upgrade is an
expand/contract pair:

- **Expand** (migrations `0002` and `0006`, online-safe): `offering_id` is
  added to `reporting_configurations`, `reporting_obligations`,
  `reporting_consumer_statuses` and the managed-delivery tables, with `''` for
  every existing account-level generation and `CHECK (offering_id = '')`. The
  keys and references the earlier release uses keep their names, and the
  widened ones are added beside them. Processes of the earlier release keep
  starting, reading and writing account-level generations (with status
  notifications too) while the new release rolls out, and a rollback works.
  No evidence row is rewritten or dropped. The applied schema versions are
  recorded in `adcp_schema_migrations` (`create_schema()` applies and records
  the ledger's own `0001`–`0003`; `python -m adcp migrate` also applies the
  later ones, which only `postgres_storage` and the notification stores need).
  Each step is transactional, serialized under the reporting schema advisory
  lock, and a no-op when re-run.
- **Contract** (migration `0007`, stopped-worker): run `python -m adcp migrate
  --contract` once every earlier-release reporting process has stopped. Plain
  `python -m adcp migrate` and `create_schema()` never contract, so the former
  is safe as a pre-deploy job. The contracted ledger is exactly a new ledger's
  catalog. Reporting subscriptions need it: before it, recording a
  subscription's generation raises `ReportingUpgradeNotContractedError` (code
  `REPORTING_UPGRADE_NOT_CONTRACTED`). After it, the earlier release cannot
  start against the ledger, so rolling back means restoring the backup.
- **Issue events** (migration `0008`, additive): `python -m adcp migrate` adds
  `reporting_issue_events`, the outbox the issue observer reads (#1258), even
  while the contract step waits. It is one new table and its indexes, so
  earlier-release processes keep running beside it; they record no issue
  events for their own transitions.

`postgres_storage(...)` starts on a ledger whose only pending migration is the
contract step. A new ledger is created contracted. A ledger that 0.2.0b1 or
0.2.0b2 already widened in place counts as contracted. The materializer,
production and status projection extensions still widen their own tables when
the new release starts: stop their earlier-release workers first. See
[Deploying the upgrade](https://github.com/adcontextprotocol/adcp-client-python/blob/next/docs/reporting-ledger-migration.md#deploying-the-upgrade).

A ledger written by 0.1.0b5 (the `(xid, seq)` account change feed, 255-character
row binding IDs, the orphan collector fence) upgrades the same way, with no
further migration: `create_schema()` re-applies the change-feed and row-storage
DDL when those options are on. See
[Upgrading a ledger that 0.1.0b5 wrote](https://github.com/adcontextprotocol/adcp-client-python/blob/next/docs/reporting-ledger-migration.md#upgrading-a-ledger-that-adcp-reporting-storage-010b5-wrote)
for the account horizon a 0.2.0b0–b2 interlude leaves behind.

## Versioning

0.x: breaking changes may land in a minor release, each with a migration note in
[CHANGELOG.md](CHANGELOG.md). The package reaches 1.0 when the storage layout
shared with the JavaScript SDK (`@adcp/reporting-storage`) is declared stable.

## Unpublished tags

`adcp-reporting-storage-v0.1.0-beta.2` and `-beta.3` exist on GitHub but were
never published to PyPI: their release commits failed `main` CI, which the
publish workflow requires. 0.1.0b4 contains both. Install 0.1.0b4 or later.

## Documentation

The guides live in the repository: [Reliable Reporting service](../../docs/reliable-reporting-service.md),
[row storage](../../docs/reporting-row-storage.md), [ledger migrations](../../docs/reporting-ledger-migration.md),
[caller-ownership migration](../../docs/reporting-caller-ownership-migration.md),
and the [migration note for the 9.0 split](../../docs/reporting-storage-package-migration.md).
