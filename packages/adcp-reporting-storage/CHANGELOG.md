# Changelog

## [0.1.0-beta.5](https://github.com/adcontextprotocol/adcp-client-python/compare/adcp-reporting-storage-v0.1.0-beta.4...adcp-reporting-storage-v0.1.0-beta.5) (2026-11-07)


### Features

* **reporting-storage:** accept 255 character row binding IDs and credential refs ([#1828](https://github.com/adcontextprotocol/adcp-client-python/issues/1828)) ([6bb34df](https://github.com/adcontextprotocol/adcp-client-python/commit/6bb34df00821e11f03aed2a018a0c5a6ccab3cdf))
* **reporting-storage:** collect orphaned row objects by listing with a clone fence ([#1835](https://github.com/adcontextprotocol/adcp-client-python/issues/1835)) ([e004f1b](https://github.com/adcontextprotocol/adcp-client-python/commit/e004f1bc55eff3bc32065a9c785f451af3813f79))
* **reporting-storage:** order the account change feed by xid and seq ([#1827](https://github.com/adcontextprotocol/adcp-client-python/issues/1827)) ([4449bc9](https://github.com/adcontextprotocol/adcp-client-python/commit/4449bc94cdd47900f39e15f5e99a35a9699d0c30))


### Performance Improvements

* **reporting-storage:** drop the per-object advisory lock where nothing pairs on it ([#1836](https://github.com/adcontextprotocol/adcp-client-python/issues/1836)) ([536b8f8](https://github.com/adcontextprotocol/adcp-client-python/commit/536b8f870f518e7ceab3c7a1d64aa0b229b75e8e))

## [0.1.0-beta.4](https://github.com/adcontextprotocol/adcp-client-python/compare/adcp-reporting-storage-v0.1.0-beta.3...adcp-reporting-storage-v0.1.0-beta.4) (2026-10-25)


### docs

* **reporting-storage:** note that 0.1.0b2 and 0.1.0b3 were not published ([#1602](https://github.com/adcontextprotocol/adcp-client-python/issues/1602)) ([0479d7d](https://github.com/adcontextprotocol/adcp-client-python/commit/0479d7d4060302ac06d32ce0f264f3d70b9a4d6f))

## [0.1.0-beta.3](https://github.com/adcontextprotocol/adcp-client-python/compare/adcp-reporting-storage-v0.1.0-beta.2...adcp-reporting-storage-v0.1.0-beta.3) (2026-10-25)


### Bug Fixes

* **reporting-storage:** surface ownership migration errors from startup ([51365c9](https://github.com/adcontextprotocol/adcp-client-python/commit/51365c9715c3b74c11b3353991525af93d04f392))

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
