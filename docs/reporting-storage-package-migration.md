# Reporting storage moved to `adcp-reporting-storage`

SDK 9.0.0 ships the Reliable Reporting *storage implementations* as a separate
distribution, `adcp-reporting-storage`, maintained in this repository and
versioned on its own line ([#1510](https://github.com/adcontextprotocol/adcp-client-python/issues/1510)).
`adcp` keeps the store protocols, the in-memory stores, the producer, the
handlers, the row encoding and the digests. The PostgreSQL stores, the chunked
row storage with its filesystem, GCS, S3 and Azure providers, the host change
feed, period retirement, the ownership-migration helpers and the PostgreSQL
service factory moved.

This is a clean cut, not a deprecation: nothing is on PyPI stable, the 9.0
betas are the only adopters, and the JavaScript SDK made the same cut during
its 16.0 beta (`@adcp/reporting-storage`). The old import paths no longer
exist, and `adcp` never imports the new package (a test enforces it).

## Install

| Before | After |
| --- | --- |
| `pip install "adcp-reporting-storage[pg]"` for the reporting stores | `pip install "adcp-reporting-storage[pg]"` (installs `adcp` too) |
| `adcp-reporting-storage[gcs]`, `adcp-reporting-storage[s3]`, `adcp-reporting-storage[azure]` | `adcp-reporting-storage[gcs]`, `[s3]`, `[azure]` |
| `adcp-reporting-storage[pg]` for `adcp.signing.PgReplayStore` and the decisioning stores | unchanged |

Pin both: `adcp-reporting-storage` depends on `adcp>=9.0.0bN,<10` and tracks
one `adcp` major.

## Imports

Every moved name is importable from the top-level `adcp_reporting_storage`
package, lazily (no driver is imported until a store is constructed). The deep
paths mirror `adcp.reporting`: `adcp.reporting.<pkg>.pg` became
`adcp_reporting_storage.<pkg>.pg`.

| Old | New |
| --- | --- |
| `adcp.reporting.ledger.PgReportingLedgerStore`, `.ledger.pg` | `adcp_reporting_storage.PgReportingLedgerStore` (`adcp_reporting_storage.ledger.pg`) |
| `adcp.reporting.ledger.PgReportingReconciliationStore`, `.ledger.delivery_pg` | `adcp_reporting_storage.PgReportingReconciliationStore` (`.ledger.delivery_pg`) |
| `adcp.reporting.materializer.PgReportingMaterializerStore` | `adcp_reporting_storage.PgReportingMaterializerStore` (`.materializer.pg`) |
| `adcp.reporting.receipts.PgReportingReceiptStore` | `adcp_reporting_storage.PgReportingReceiptStore` (`.receipts.pg`) |
| `adcp.reporting.feed.PgReportingFeedStore` | `adcp_reporting_storage.PgReportingFeedStore` (`.feed.pg`) |
| `adcp.reporting.projection.PgReportingProjectionStore`, `PgReportingStatusProjection`, `PgReportingProjectionOutbox` | `adcp_reporting_storage.*` (`.projection.pg`, `.projection.notifications`) |
| `adcp.reporting.production.PgReportingProductionStore`, `PgReportingProductionOutbox` | `adcp_reporting_storage.*` (`.production.pg`) |
| `adcp.reporting.outbox.PgReportingOutbox`, `PgReportingStatusOutbox`, `PgStatusNotificationStore`, `PgReportingActivityUnionStore` | `adcp_reporting_storage.*` (`.outbox.pg`, `.outbox.status_pg`, `.outbox.status_activity_pg`) |
| `adcp.reporting.submissions.PgReportingSubmissionIntentStore` | `adcp_reporting_storage.PgReportingSubmissionIntentStore` (`.submissions.pg`) |
| `adcp.reporting.inline_storage.PgReportingStagingStore`, `PgReportingSealStore` | `adcp_reporting_storage.inline_storage.*` (also top-level) |
| `adcp.reporting.migration` (`migrate_legacy_reporting`, `backfill_legacy_reporting`, `replace_empty_legacy_reporting`, `replace_empty_legacy_reporting_sync`, `replace_empty_owned_reporting`, `replace_empty_owned_reporting_sync`, `ReportingOwnershipBackfill`, `ReportingOwnershipMigrationError`, …) | `adcp_reporting_storage.migration` (also top-level) |
| `adcp.reporting.ledger.row_storage*`, `row_write`, `row_migration`, `row_retention`, `period_retirement`, `change_feed` | `adcp_reporting_storage.ledger.<same module>` |
| `adcp.reporting.ledger.ReportingRowStorageOptionsV1`, `ReportingObjectRowBindingV1`, `ReportingPostgresRowBindingV1`, `ReportingRowBindingSelectionInputV1`, `ReportingRowIntentSweepV1`, `ReportingRowPruneV1`, `ReportingPeriodRetirementV1`, `ReportingChange*V1`, `ReportingChangeCursorExpiredError` | `adcp_reporting_storage.<same name>` |
| `adcp.reporting.ledger.row_storage_object.ReportingRowObjectProviderV1`, `create_filesystem_reporting_row_object_provider_v1`, `run_reporting_row_object_provider_conformance_v1`, `reporting_period_date_v1` | `adcp_reporting_storage.<same name>` (`.ledger.row_storage_object`) |
| `adcp.reporting.ledger.row_storage_gcs.create_gcs_reporting_row_object_provider_v1`, `..._s3...`, `..._azure...` | `adcp_reporting_storage.<same name>` |
| `adcp.reporting.ledger.status_snapshot.read_snapshot_on`, `settle_snapshot_on`, `persist_replay_lifecycles_on`, `apply_intents_on` | `adcp_reporting_storage.ledger.status_snapshot.<same name>` |
| `adcp.reporting.outbox._schema`, `status_schema`; `adcp.reporting.<pkg>.schema` validators and `required_*.json` manifests | `adcp_reporting_storage.outbox._schema`, `.status_schema`; `adcp_reporting_storage.<pkg>.schema` |
| `postgres_reliable_reporting_service(pool=..., ...)` | `adcp_reporting_storage.postgres_reliable_reporting_service(pool=..., ...)` (same keywords) |
| DDL at `adcp/reporting/ledger/*.sql` | `adcp_reporting_storage/ledger/*.sql`; `importlib.resources.files("adcp_reporting_storage.ledger")` |

What stays in `adcp.reporting.ledger`: `ReportingLedgerStore` and every other
store protocol, `InMemoryReportingLedgerStore`, `ReportingProducer`,
`ReportingStatusHandler`, `ReportingRowStoreError` and
`ReportingRowStoreErrorCode` (store-contract errors every implementation
raises), `ReportingRowPage`, `row_encoding` (the canonical row bytes and
digests), and the `delivery_changes` reconciliation feed port.

## New protocols in `adcp`

The protocol layer used to recognize the PostgreSQL classes by `type(...)`.
It now asks the store. If you implement a store, these are additive and
optional; the in-memory stores implement them, and `adcp-reporting-storage`
implements them for PostgreSQL:

- `adcp.reporting.production.store.ReportingProductionStore` and
  `ReportingProductionProjection`: what `ReportingProductionSupport` and
  `ReportingProductionOptions` compose over (queues, projection, schema
  readiness, delivery windows, maintenance passes).
- `adcp.reporting.outbox.durable.ReportingDurableNotificationLedger` and
  `ReportingDurableStatusStore`: how the capability checks prove a durable
  notification, activity and status chain.
- `adcp.reporting.ledger.ReportingRowStagingStore`,
  `ReportingSourcePublicationStore`, `ReportingWorkerRetryPolicy`: the
  optional hooks the producer used to reach through `getattr`
  ([#1489](https://github.com/adcontextprotocol/adcp-client-python/issues/1489),
  minimal form; the staged-handle variant is a follow-up).
- `ReliableReportingService(...)` takes `staging=`, `seals=` and
  `initialize_source_storage=` so a storage distribution can supply durable
  source stores without reaching into the service.

Public attributes replaced private ones where the composition reads them:
`store.production_support` (was `_production_support`),
`store.projection_read_policy` / `projection_read_escalation`,
`store.activate_production(...)` (was `_activate_production`),
`store.source_publication(...)` (was `_source_publication`),
`store.worker_lock_errors()` (was `_worker_lock_errors`), and
`outbox.next_due_account(delivery=..., now=...)`.

## Tests and tooling

The conformance tests keep living under `tests/` and import
`adcp_reporting_storage`; CI installs both distributions in every lane
(`uv pip install -e ".[dev,pg]" -e "./packages/adcp-reporting-storage[pg,gcs,s3,azure]"`).
`make lint`, `make typecheck` and `make build` cover both. The public-API
snapshot (`tests/fixtures/public_api_snapshot.json`) now records
`adcp.reporting`, `adcp.reporting.ledger` and `adcp_reporting_storage`
([#1490](https://github.com/adcontextprotocol/adcp-client-python/issues/1490)).
