# Changelog

## [1.3.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.2.1...v1.3.0) (2025-11-10)


### Features

* add no-auth test agent helpers ([#29](https://github.com/adcontextprotocol/adcp-client-python/issues/29)) ([baa5608](https://github.com/adcontextprotocol/adcp-client-python/commit/baa56082d8cac5f3fc48da5905eeaabb7cf0473a))
* add test helpers for quick testing ([#27](https://github.com/adcontextprotocol/adcp-client-python/issues/27)) ([80dee92](https://github.com/adcontextprotocol/adcp-client-python/commit/80dee92c93635d0b6665393eacc5a1d36e4480bd))

## [1.2.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.2.0...v1.2.1) (2025-11-09)


### Documentation

* add Python version requirement note to README ([#25](https://github.com/adcontextprotocol/adcp-client-python/issues/25)) ([b2e5233](https://github.com/adcontextprotocol/adcp-client-python/commit/b2e5233a482d48251df875e77a70523972bfc988))

## [1.2.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.1.0...v1.2.0) (2025-11-08)


### Features

* improve type generation with discriminated union support ([#21](https://github.com/adcontextprotocol/adcp-client-python/issues/21)) ([54da596](https://github.com/adcontextprotocol/adcp-client-python/commit/54da5967f0962b814460ce53fc35494af2f023b6))

## [1.1.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.5...v1.1.0) (2025-11-07)


### Features

* batch preview API with 5-10x performance improvement ([#18](https://github.com/adcontextprotocol/adcp-client-python/issues/18)) ([813df8a](https://github.com/adcontextprotocol/adcp-client-python/commit/813df8a5c27e44cccf832552ad331c364c27925f))


### Bug Fixes

* improve MCP adapter cleanup on connection failures ([#19](https://github.com/adcontextprotocol/adcp-client-python/issues/19)) ([40d83f3](https://github.com/adcontextprotocol/adcp-client-python/commit/40d83f3ac727126a329784638a98094670ab3b45))

## [1.0.5](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.4...v1.0.5) (2025-11-07)


### Bug Fixes

* return both message and structured content in MCP responses ([#16](https://github.com/adcontextprotocol/adcp-client-python/issues/16)) ([696a3aa](https://github.com/adcontextprotocol/adcp-client-python/commit/696a3aa94dd44ee303577efceefb038ac3bac06a))

## [1.0.4](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.3...v1.0.4) (2025-11-07)


### Bug Fixes

* handle Pydantic TextContent objects in MCP response parser ([#14](https://github.com/adcontextprotocol/adcp-client-python/issues/14)) ([6b60365](https://github.com/adcontextprotocol/adcp-client-python/commit/6b60365ffd0c084b3989d38e548e0d2de8c85c57))

## [1.0.3](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.2...v1.0.3) (2025-11-07)


### Bug Fixes

* parse list_creative_formats response into structured type ([#12](https://github.com/adcontextprotocol/adcp-client-python/issues/12)) ([15b5395](https://github.com/adcontextprotocol/adcp-client-python/commit/15b53950ed2ed1f208fb93b73f0743725fb0e718))

## [1.0.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.1...v1.0.2) (2025-11-06)


### Bug Fixes

* correct tool name in list_creative_formats method ([#10](https://github.com/adcontextprotocol/adcp-client-python/issues/10)) ([d9eff68](https://github.com/adcontextprotocol/adcp-client-python/commit/d9eff68df85a018eefd3b1a0d1a4d763d9aa106b))

## [1.0.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.0.0...v1.0.1) (2025-11-06)


### Bug Fixes

* use correct PYPY_API_TOKEN secret for PyPI publishing ([#8](https://github.com/adcontextprotocol/adcp-client-python/issues/8)) ([b48a33a](https://github.com/adcontextprotocol/adcp-client-python/commit/b48a33aaafa9f407b375036b7e656e63ed37544a))

## [1.0.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v0.1.2...v1.0.0) (2025-11-06)


### ⚠ BREAKING CHANGES

* All client methods now require typed request objects. The legacy kwargs API has been removed for a cleaner, more type-safe interface.
* All client methods now require typed request objects. The legacy kwargs API has been removed for a cleaner, more type-safe interface.
* All client methods now require typed request objects. The legacy kwargs API has been removed for a cleaner, more type-safe interface.

### Features

* complete Python AdCP SDK with typed API and auto-generated types ([#5](https://github.com/adcontextprotocol/adcp-client-python/issues/5)) ([bc8ebc9](https://github.com/adcontextprotocol/adcp-client-python/commit/bc8ebc957349550887b0d329fba02d5222a311ef))


### Bug Fixes

* correct PyPI API token secret name ([#6](https://github.com/adcontextprotocol/adcp-client-python/issues/6)) ([eae30ce](https://github.com/adcontextprotocol/adcp-client-python/commit/eae30ceb9538a4ff2baf0a0a9a944b9e5ae0c5a0))


### Reverts

* restore correct PYPY_API_TOKEN secret name ([#7](https://github.com/adcontextprotocol/adcp-client-python/issues/7)) ([330f484](https://github.com/adcontextprotocol/adcp-client-python/commit/330f48449dce18356e94bf1f95c8e4e4d4c59178))


### Documentation

* update PyPI setup guide with correct secret name and current status ([085b961](https://github.com/adcontextprotocol/adcp-client-python/commit/085b961ef6d49050a9dc4bcdd956ff29d2955aed))

## [0.1.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v0.1.1...v0.1.2) (2025-11-05)


### Bug Fixes

* correct secret name from PYPI_API_TOKEN to PYPY_API_TOKEN ([0b7599d](https://github.com/adcontextprotocol/adcp-client-python/commit/0b7599d09321c8a12e934a249817816f60b92372))

## [0.1.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v0.1.0...v0.1.1) (2025-11-05)


### Bug Fixes

* remove deprecated package-name parameter from release-please config ([28d8154](https://github.com/adcontextprotocol/adcp-client-python/commit/28d8154a8185e6c841804b39e7381f6bb22bde03))

## 0.1.0 (2025-11-05)


### Features

* add automated versioning and PyPI publishing ([e7f8bbb](https://github.com/adcontextprotocol/adcp-client-python/commit/e7f8bbba5169a642f05b99d018c17491f4a86982))


### Documentation

* add comprehensive PyPI publishing setup guide ([dcc8135](https://github.com/adcontextprotocol/adcp-client-python/commit/dcc81354ca322eed92b879c3aa26a78d1f8ba3de))
