# Changelog

## [0.2.0-beta.1](https://github.com/adcontextprotocol/adcp-client-python/compare/adcp-reporting-storage-v0.2.0-beta.0...adcp-reporting-storage-v0.2.0-beta.1) (2026-11-02)


### Features

* **reporting-storage:** add PostgreSQL stores for reporting subscriptions ([#1724](https://github.com/adcontextprotocol/adcp-client-python/issues/1724)) ([46a1e01](https://github.com/adcontextprotocol/adcp-client-python/commit/46a1e01a65e01d547a22a05a3858709c3309a011)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting-storage:** deliver reporting subscriptions through the PostgreSQL production store ([333772f](https://github.com/adcontextprotocol/adcp-client-python/commit/333772fafca5fc14b6ee6be4d7937757ff3550d3))
* **reporting-storage:** deliver reporting subscriptions through the PostgreSQL production store ([2beb3b2](https://github.com/adcontextprotocol/adcp-client-python/commit/2beb3b24b4947d57598e1a4b37a4db72053fd6b5)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting-storage:** one storage decision with versioned migrations ([c0753d6](https://github.com/adcontextprotocol/adcp-client-python/commit/c0753d6ef5a5794905892668b6b9745fc9f24dd2))
* **reporting-storage:** one storage decision with versioned migrations ([99fe10b](https://github.com/adcontextprotocol/adcp-client-python/commit/99fe10b2c4e7e434040ac53b1fc7fcf3fd8ce780)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** check reporting tasks against the current subscription grant ([d34633c](https://github.com/adcontextprotocol/adcp-client-python/commit/d34633c7945bd8119cf501238aa9f7ac0ce1d9f3))
* **reporting:** deliver reporting subscriptions through production managed delivery ([6015c72](https://github.com/adcontextprotocol/adcp-client-python/commit/6015c72582809c589b3134e98fb0ac5e72ed4c7b))
* **reporting:** hold reporting-subscription owed sets in the PostgreSQL ledger ([4808ee5](https://github.com/adcontextprotocol/adcp-client-python/commit/4808ee59f62d41c4988fefd792262838671a32dc))
* **reporting:** hold reporting-subscription owed sets in the PostgreSQL ledger ([7c080b1](https://github.com/adcontextprotocol/adcp-client-python/commit/7c080b14de722111dca989ba8846a2b33cb4d2dc))
* **reporting:** hold reporting-subscription owed sets in the reporting ledger ([e71bd79](https://github.com/adcontextprotocol/adcp-client-python/commit/e71bd79e31c8b5801057b7e3e9fdbf36dc3f67df))
* **reporting:** hold reporting-subscription owed sets in the reporting ledger ([211c0db](https://github.com/adcontextprotocol/adcp-client-python/commit/211c0db057e90dbf47b142165a930e95b84c64b3))
* **reporting:** mirror the TypeScript SDK's advanced path, engine errors and readback name ([1b8f704](https://github.com/adcontextprotocol/adcp-client-python/commit/1b8f7040b8127584288b940e89d2b62a52f4f763))
* **reporting:** mirror the TypeScript SDK's advanced path, engine errors and readback name ([f5c119d](https://github.com/adcontextprotocol/adcp-client-python/commit/f5c119da9e67ecf85fe071bdef43e26cd4b63742)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** read deactivated subscriptions as inactive in coverage ([e840da8](https://github.com/adcontextprotocol/adcp-client-python/commit/e840da86b66f7db94b9afa9e6f5ef398f734cc47))
* **reporting:** serve reporting subscriptions from ReliableReportingService ([f2559ca](https://github.com/adcontextprotocol/adcp-client-python/commit/f2559cadb50f1bb9a0617e051750315925d084b8))
* **reporting:** subscription rows carry account_id and delivery re-checks authority ([e5d1c9a](https://github.com/adcontextprotocol/adcp-client-python/commit/e5d1c9a6974078f8f31921dc7afe4cac4e61372f))


### Bug Fixes

* **reporting-storage:** keep adcp free of imports of the storage distribution ([7dc6905](https://github.com/adcontextprotocol/adcp-client-python/commit/7dc6905e75b8d9c4cc346207dc16faaa43dcb115)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** admit path_template in the closed reconciliation payload ([801061f](https://github.com/adcontextprotocol/adcp-client-python/commit/801061f6a69cd626a6019b23b83493fabdd63861))
* **reporting:** key status checkpoints by resolved offering ([d490481](https://github.com/adcontextprotocol/adcp-client-python/commit/d49048161de72fe3499da1938ca406a304e278e1))
* **reporting:** key status checkpoints by resolved offering ([9494c95](https://github.com/adcontextprotocol/adcp-client-python/commit/9494c95b56bf93b21a28fbf2b95b97b0d1b4d728))
* **reporting:** repin the reconciliation guard and the public API snapshot ([33a7934](https://github.com/adcontextprotocol/adcp-client-python/commit/33a79343ad98113c919427aa2816f00b8f3b1883))

## [0.2.0-beta.0](https://github.com/adcontextprotocol/adcp-client-python/compare/adcp-reporting-storage-v0.1.0-beta.4...adcp-reporting-storage-v0.2.0-beta.0) (2026-10-27)


### chore

* **reporting-storage:** cut 0.2.0-beta.N pre-releases from next ([dc0928b](https://github.com/adcontextprotocol/adcp-client-python/commit/dc0928bfeb8bf105f7cacbf46b71436964877f00))

## Changelog (next)

The SDK 10 line of `adcp-reporting-storage`, released from `next` as
`0.2.0-beta.N` pre-releases beside `adcp` `10.0.0-beta.N`. The SDK 9 line's
changelog is `CHANGELOG.md`, released from `main`. Release Please appends the
entries below.
