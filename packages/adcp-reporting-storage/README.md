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
`adcp` release of the contract this package implements, so an incompatible pair
does not resolve; if one is forced, building a PostgreSQL store refuses at
startup, naming both versions and the release to install.

| adcp-reporting-storage | storage contract | adcp |
|---|---|---|
| 0.1.0b4 (SDK 9 line) | 1 | `>=9.0.0,<10` |
| 0.2.0b0 | 1 | `>=9.0.0,<11` (**do not pair with 10.0.0b4+**, [#1763](https://github.com/adcontextprotocol/adcp-client-python/issues/1763)) |
| 0.2.0b1+ | 2 | `>=10.0.0b4,<11` |

`adcp` never imports this package; a test in the main repository enforces that.

### Upgrading a ledger created by 0.1.0b4 or 0.2.0b0

Both `PgReportingLedgerStore.create_schema()` (what `ReliableReportingService`
runs at startup when given `store=`) and `python -m adcp migrate` upgrade an
existing ledger in place: `offering_id` is added to `reporting_configurations`,
`reporting_obligations`, `reporting_consumer_statuses` and the managed-delivery
tables, backfilled with `''` for every existing account-level generation, the
generation keys and unique indexes are widened, and the applied schema
versions are recorded in `adcp_schema_migrations`. No evidence row is rewritten
or dropped. The upgrade is transactional, serialized under the reporting schema
advisory lock, and a no-op when re-run. It is a **stopped-worker** upgrade:
stop every reporting worker of the earlier release first (its configuration
writes name the four-column key), run the upgrade, then start the new release.

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
