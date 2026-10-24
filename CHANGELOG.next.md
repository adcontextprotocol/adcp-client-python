# Changelog

## [10.0.0-alpha.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v9.0.0-beta.3...v10.0.0-alpha.0) (2026-10-24)


### ⚠ BREAKING CHANGES

* **signing:** adcp.signing.b64url_decode accepts only RFC 7515 base64url (no padding, no standard-alphabet characters, no non-zero trailing bits) and raises ValueError for anything else. public_key_from_jwk reads a JWK through joserfc, so its ValueError text changes. private_key_from_jwk raises ValueError when a JWK's x (or y) isn't the public key of its d. adcp.signing.publisher_pins.jwk_thumbprint returns the RFC 7638 thumbprint as a base64url str instead of the digest bytes.
* **types:** adcp.types.generated_poc no longer imports. Use adcp.types.domains, or run adcp migrate v3-to-v4 --apply.
* **reporting:** row storage tables, provider inputs and locators are renamed to the AdCP names above, REPORTING_ROW_ENCODING_V1 is replaced by REPORTING_ROW_FORMAT_V1 and REPORTING_ROW_ALGORITHM_V1, and {period_date} renders the source timezone date as YYYY-MM-DD instead of the UTC date as yyyy/MM/dd, and a custom key template without {revision_key} is refused unless its binding is read_only. create_schema migrates existing databases; stop older writers first.

### ci

* **release:** cut 10.0.0-alpha.N pre-releases from next ([df3ee2c](https://github.com/adcontextprotocol/adcp-client-python/commit/df3ee2c1221169ba1cd9df425a8c5be250c53e9a))


### Features

* **client:** cap agent response size by default ([#1504](https://github.com/adcontextprotocol/adcp-client-python/issues/1504)) ([d5195a9](https://github.com/adcontextprotocol/adcp-client-python/commit/d5195a9525239124416741808addcbcf0f0a8a98))
* **identity:** add experimental AdCP 3.3 agent identity ([#1502](https://github.com/adcontextprotocol/adcp-client-python/issues/1502)) ([3961d1c](https://github.com/adcontextprotocol/adcp-client-python/commit/3961d1cf7a235c4d45673370521b1aec9bc4c496))
* **identity:** verify JWS artifacts on the issuer's Web Bot Auth key directory ([#1543](https://github.com/adcontextprotocol/adcp-client-python/issues/1543)) ([233ebee](https://github.com/adcontextprotocol/adcp-client-python/commit/233ebeefd1bd44640a9fa983299c6a65410b2356))
* **property-lists:** unify resolution and validate seller inventory ([#1532](https://github.com/adcontextprotocol/adcp-client-python/issues/1532)) ([734bf3a](https://github.com/adcontextprotocol/adcp-client-python/commit/734bf3acde31fb1ea7b96867710c08087384737d))
* **reporting:** add current-revision host reads ([#1473](https://github.com/adcontextprotocol/adcp-client-python/issues/1473)) ([13ca2c5](https://github.com/adcontextprotocol/adcp-client-python/commit/13ca2c502818777bf1eef2f2cd5e8bfbac196dcf))
* **reporting:** add GCS, S3 and Azure Blob row providers ([#1468](https://github.com/adcontextprotocol/adcp-client-python/issues/1468)) ([fde127d](https://github.com/adcontextprotocol/adcp-client-python/commit/fde127db8992bc6f62303ebbfcbf8f0617be33f4))
* **reporting:** add the shared host change feed and registered consumers ([#1472](https://github.com/adcontextprotocol/adcp-client-python/issues/1472)) ([b7012e0](https://github.com/adcontextprotocol/adcp-client-python/commit/b7012e05591f3e7823314ab39d65cb1cc723324a))
* **reporting:** align row storage with AdCP stored names and the period date rule ([#1485](https://github.com/adcontextprotocol/adcp-client-python/issues/1485)) ([47084e8](https://github.com/adcontextprotocol/adcp-client-python/commit/47084e83a4a2cfd879db4127e26c3fb54fd73dd8))
* **reporting:** migrate legacy inline rows to verified chunks ([#1466](https://github.com/adcontextprotocol/adcp-client-python/issues/1466)) ([bf7e3a6](https://github.com/adcontextprotocol/adcp-client-python/commit/bf7e3a62b38975ed037bc8dbd440bfb2ebe440d4))
* **reporting:** retire expired periods with tombstones and holds ([#1474](https://github.com/adcontextprotocol/adcp-client-python/issues/1474)) ([c527b18](https://github.com/adcontextprotocol/adcp-client-python/commit/c527b1866bf544ca0926eb844b316136a2172a5c))
* **reporting:** schedule retention in the production worker ([#1476](https://github.com/adcontextprotocol/adcp-client-python/issues/1476)) ([5c23598](https://github.com/adcontextprotocol/adcp-client-python/commit/5c23598b420725987c3ed0f79c923ead19f1d6be))
* **reporting:** store revision rows as verified chunks in PostgreSQL ([#1464](https://github.com/adcontextprotocol/adcp-client-python/issues/1464)) ([54a133e](https://github.com/adcontextprotocol/adcp-client-python/commit/54a133ed8e1723e20f1d575b68af88e214afede3))
* **reporting:** sweep abandoned uploads and prune expired row sets ([#1469](https://github.com/adcontextprotocol/adcp-client-python/issues/1469)) ([e32daba](https://github.com/adcontextprotocol/adcp-client-python/commit/e32daba5c78b5c49ebeee25730b42acb0a1c72d6))
* **reporting:** write row chunks to object storage with fenced uploads ([#1467](https://github.com/adcontextprotocol/adcp-client-python/issues/1467)) ([83f62c9](https://github.com/adcontextprotocol/adcp-client-python/commit/83f62c9bcef7cca65abae376fe796e6df13eb064))
* **types:** remove the adcp.types.generated_poc alias in v10 ([#1536](https://github.com/adcontextprotocol/adcp-client-python/issues/1536)) ([893720b](https://github.com/adcontextprotocol/adcp-client-python/commit/893720b649bf5ff5b7ac45d5e5c2c724469fbd98))


### Bug Fixes

* **governance:** require crit markers with their claims, and run the signing tests CI skips ([#1539](https://github.com/adcontextprotocol/adcp-client-python/issues/1539)) ([15c85cf](https://github.com/adcontextprotocol/adcp-client-python/commit/15c85cf6e5de6b1b401c688d3e3e8470a47ba449))
* **identity:** a grantee that is not a registrable domain covers no claim ([#1542](https://github.com/adcontextprotocol/adcp-client-python/issues/1542)) ([6cb3c8a](https://github.com/adcontextprotocol/adcp-client-python/commit/6cb3c8a2be7823b3f399cd7351dfa62154439d2c))
* **identity:** align AdCP 3.3 agent identity with the final spec and the Go and TS SDKs ([#1514](https://github.com/adcontextprotocol/adcp-client-python/issues/1514)) ([2b08ced](https://github.com/adcontextprotocol/adcp-client-python/commit/2b08cedcf5b75e7a364363e1cfcea676bd241456))
* **identity:** implicit grant authority extends to the brand's house ([#1565](https://github.com/adcontextprotocol/adcp-client-python/issues/1565)) ([4b8b7e4](https://github.com/adcontextprotocol/adcp-client-python/commit/4b8b7e48ce9a695431c57317adbcbb77ee01d220))
* **protocol:** adopt signed AdCP 3.2.4 on SDK 9 beta ([#1479](https://github.com/adcontextprotocol/adcp-client-python/issues/1479)) ([0ad2a8f](https://github.com/adcontextprotocol/adcp-client-python/commit/0ad2a8fae218ae927b75ed6ac38ddb5107822350))
* **reporting:** answer unreadable exact revision reads with SERVICE_UNAVAILABLE ([#1471](https://github.com/adcontextprotocol/adcp-client-python/issues/1471)) ([623b2c1](https://github.com/adcontextprotocol/adcp-client-python/commit/623b2c1603d2c59dc3d5429f7e44c469a3550fcb))
* **reporting:** keep feed consumer checkpoints forward-only ([#1515](https://github.com/adcontextprotocol/adcp-client-python/issues/1515)) ([0de3f57](https://github.com/adcontextprotocol/adcp-client-python/commit/0de3f57c7e3a434c6d774e880663ca8c4908841c)), closes [#1509](https://github.com/adcontextprotocol/adcp-client-python/issues/1509)
* **reporting:** keep retention moving past stuck work and make it visible ([#1482](https://github.com/adcontextprotocol/adcp-client-python/issues/1482)) ([4623104](https://github.com/adcontextprotocol/adcp-client-python/commit/462310439d05b92cdfe510b9aac571a8a9f28f0c))
* **reporting:** reject a change page that crossed a concurrent prune ([#1516](https://github.com/adcontextprotocol/adcp-client-python/issues/1516)) ([73d1261](https://github.com/adcontextprotocol/adcp-client-python/commit/73d12611effa9adeb8a588992428a60864e6a006)), closes [#1508](https://github.com/adcontextprotocol/adcp-client-python/issues/1508)
* **reporting:** truncate account feed pages at the first in-flight change and match JS backoff ([#1483](https://github.com/adcontextprotocol/adcp-client-python/issues/1483)) ([21f4216](https://github.com/adcontextprotocol/adcp-client-python/commit/21f4216addc5e39f30b076288889b57f8d1cb455))
* **server:** advertise compact output schemas so discovery is servable ([#1507](https://github.com/adcontextprotocol/adcp-client-python/issues/1507)) ([d908aaa](https://github.com/adcontextprotocol/adcp-client-python/commit/d908aaa98caa88e5b0cd992037eb5010dc009da1))
* **server:** collect stranded defs, memoize the escape hatch, soften a claim ([#1513](https://github.com/adcontextprotocol/adcp-client-python/issues/1513)) ([fc285b0](https://github.com/adcontextprotocol/adcp-client-python/commit/fc285b057406f0e715412d1d53bb86a281503fb2)), closes [#1507](https://github.com/adcontextprotocol/adcp-client-python/issues/1507)
* **signing:** match standard Web Bot Auth, and finish the library migration ([#1537](https://github.com/adcontextprotocol/adcp-client-python/issues/1537)) ([09db189](https://github.com/adcontextprotocol/adcp-client-python/commit/09db1893c7b45ca2ca07d6a8db90bb59f05ec089))
* **signing:** work with http-sf 1.5.0 and http-message-signatures 2.2.0 ([#1568](https://github.com/adcontextprotocol/adcp-client-python/issues/1568)) ([cf97bd2](https://github.com/adcontextprotocol/adcp-client-python/commit/cf97bd26253fcae0c59573bcf33f6f9fb138ee5c))
* **types:** model macro_resolution_capabilities on Format ([#1505](https://github.com/adcontextprotocol/adcp-client-python/issues/1505)) ([eb66de8](https://github.com/adcontextprotocol/adcp-client-python/commit/eb66de84c7445abe0de185947d886290b08eb14f))
* **types:** preserve custom targeting validation errors ([#1484](https://github.com/adcontextprotocol/adcp-client-python/issues/1484)) ([275e520](https://github.com/adcontextprotocol/adcp-client-python/commit/275e520822eaa280f28965ade8e20085f78cfd57))


### Performance Improvements

* **types:** reduce canonical creative serialization overhead ([#1521](https://github.com/adcontextprotocol/adcp-client-python/issues/1521)) ([bde770b](https://github.com/adcontextprotocol/adcp-client-python/commit/bde770b76ab9c07fd6e8bb11394dd868ddb09984))


### Reverts

* move experimental AdCP 3.3 identity from main to next ([#1534](https://github.com/adcontextprotocol/adcp-client-python/issues/1534)) ([1e6d4b9](https://github.com/adcontextprotocol/adcp-client-python/commit/1e6d4b99b5340a5862e2429425354a1b70c27e48))
