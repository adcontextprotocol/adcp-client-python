# Changelog

## [0.1.0-beta.2](https://github.com/adcontextprotocol/adcp-client-python/compare/adcp-reporting-storage-v0.1.0-beta.1...adcp-reporting-storage-v0.1.0-beta.2) (2026-10-24)


### Features

* **reporting-storage:** let the Postgres service factory build a subclass ([dad5e8d](https://github.com/adcontextprotocol/adcp-client-python/commit/dad5e8d9668e0ebd3ced406780e659b42ee22001)), closes [#1588](https://github.com/adcontextprotocol/adcp-client-python/issues/1588)

## [0.1.0-beta.1](https://github.com/adcontextprotocol/adcp-client-python/compare/adcp-reporting-storage-v0.1.0-beta.0...adcp-reporting-storage-v0.1.0-beta.1) (2026-10-24)


### ⚠ BREAKING CHANGES

* **reporting:** the PostgreSQL reporting stores, the row storage providers, the change feed, the ownership-migration helpers and `ReliableReportingService.postgres` leave `adcp`; install `adcp-reporting-storage[pg]` (plus `gcs`/`s3`/`azure`) and import them from `adcp_reporting_storage`. The `adcp[reporting-gcs]`, `[reporting-s3]` and `[reporting-azure]` extras are removed. See docs/reporting-storage-package-migration.md.

### Features

* **reporting:** ship the Reliable Reporting storage as adcp-reporting-storage ([#1557](https://github.com/adcontextprotocol/adcp-client-python/issues/1557)) ([d59eee9](https://github.com/adcontextprotocol/adcp-client-python/commit/d59eee9da4d246557bfacf42119729e1d68e78a7))


### Bug Fixes

* **reporting-storage:** require the adcp release that contains the split ([#1577](https://github.com/adcontextprotocol/adcp-client-python/issues/1577)) ([a2315b7](https://github.com/adcontextprotocol/adcp-client-python/commit/a2315b79d31bf257b42fb09d9140abc66fce604b))

## Changelog

## Unreleased

### Features

* **reporting-storage:** first release. The PostgreSQL ledger, reconciliation,
  materializer, receipt, feed, projection, production, outbox, status, inline
  storage and buyer submission stores, the chunked row storage with its
  filesystem, Google Cloud Storage, Amazon S3 and Azure Blob providers, the
  host change feed, period retirement, row migration and the caller-ownership
  migration helpers, moved here from `adcp.reporting` (adcp-client-python#1510).
