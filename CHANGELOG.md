# Changelog

## [3.2.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.1.0...v3.2.0) (2026-02-04)


### Features

* export A2UI types from public API ([#125](https://github.com/adcontextprotocol/adcp-client-python/issues/125)) ([acc2239](https://github.com/adcontextprotocol/adcp-client-python/commit/acc223961e8b0f3108e9b14dd90b3376533269cc))

## [3.1.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.0.0...v3.1.0) (2026-01-26)


### Features

* export PricingOption and other union type aliases from main package ([#121](https://github.com/adcontextprotocol/adcp-client-python/issues/121)) ([a1c61e7](https://github.com/adcontextprotocol/adcp-client-python/commit/a1c61e7e5be8d79353c532c710586598fc562bc8)), closes [#120](https://github.com/adcontextprotocol/adcp-client-python/issues/120)

## [3.0.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.19.0...v3.0.0) (2026-01-26)


### ⚠ BREAKING CHANGES

* CpmAuctionPricingOption, CpmFixedRatePricingOption, VcpmAuctionPricingOption, VcpmFixedRatePricingOption have been consolidated into CpmPricingOption and VcpmPricingOption respectively.

### Features

* add V3 protocol support (Governance, Content Standards, SI, CLI) ([#117](https://github.com/adcontextprotocol/adcp-client-python/issues/117)) ([dfec179](https://github.com/adcontextprotocol/adcp-client-python/commit/dfec179ac58eb14f9a69ce939d4d5fe048bb6b04))

## [2.19.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.18.0...v2.19.0) (2026-01-14)


### Features

* fix PackageRequest schema inconsistency in adcp ([de2b4bf](https://github.com/adcontextprotocol/adcp-client-python/commit/de2b4bf144d2fc698410714cd0365eb99b427562))
* sync schemas with ADCP 2.6 package updates ([a6c55db](https://github.com/adcontextprotocol/adcp-client-python/commit/a6c55dbe269010909a19fb0ef8cff4652d68aca0))

## [2.18.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.17.0...v2.18.0) (2026-01-09)


### Features

* add deprecated field metadata injection and CLI warnings ([8947090](https://github.com/adcontextprotocol/adcp-client-python/commit/89470901617eece1bb9e6bb3ca6d78d69b4d0813))
* add format asset utilities and deprecation warnings for assets_required migration ([c3c379a](https://github.com/adcontextprotocol/adcp-client-python/commit/c3c379aaf47a62be29e310de5df59299ab5983c0))
* upgrade to AdCP protocol 2.6.0 ([245a7d3](https://github.com/adcontextprotocol/adcp-client-python/commit/245a7d3dd40d139e52da336305d4c82db23726b4))


### Bug Fixes

* rename format_ to format in FieldModel enum ([1a6ab9a](https://github.com/adcontextprotocol/adcp-client-python/commit/1a6ab9a1528da3c37e7dce8b7ad72cfd416c190b))
* resolve import sorting lint error in generated _ergonomic.py ([45350d1](https://github.com/adcontextprotocol/adcp-client-python/commit/45350d1a33b8fe6823b883bccb5bff07d388f596))
* resolve linter errors (import sorting, line length) ([4a53e33](https://github.com/adcontextprotocol/adcp-client-python/commit/4a53e33dd34254b34a39737bba9e0ea0c6b9960c))
* shorten comment line to pass lint ([7a2e30e](https://github.com/adcontextprotocol/adcp-client-python/commit/7a2e30ed4b9ab5ef6141662356196e9f7bc0b8c7))

## [2.17.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.16.0...v2.17.0) (2025-12-30)


### Features

* add protocol field extraction for A2A responses ([#110](https://github.com/adcontextprotocol/adcp-client-python/issues/110)) ([ae71b65](https://github.com/adcontextprotocol/adcp-client-python/commit/ae71b65bec03f7f1634d1d4eb6a45c58a657a0b2))

## [2.16.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.15.0...v2.16.0) (2025-12-21)


### Features

* extend type ergonomics to response types ([#106](https://github.com/adcontextprotocol/adcp-client-python/issues/106)) ([d31c4d8](https://github.com/adcontextprotocol/adcp-client-python/commit/d31c4d8295c9652f54889dc2d4f2e8038f03a812))

## [2.15.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.14.0...v2.15.0) (2025-12-21)


### Features

* improve type ergonomics for library consumers ([#103](https://github.com/adcontextprotocol/adcp-client-python/issues/103)) ([75dec68](https://github.com/adcontextprotocol/adcp-client-python/commit/75dec68ac467e9fca250acb53af8ade4e326d066))

## [2.14.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.13.0...v2.14.0) (2025-12-19)


### Features

* add IPR agreement workflow using centralized AdCP signatures ([#101](https://github.com/adcontextprotocol/adcp-client-python/issues/101)) ([a33c5d6](https://github.com/adcontextprotocol/adcp-client-python/commit/a33c5d6d235546bef8b3858ceb6cd90148377228))
* update schemas ([436f754](https://github.com/adcontextprotocol/adcp-client-python/commit/436f75414e954476f3cf69a0106a3e8f54458a64))
* update webhook handling ([874d170](https://github.com/adcontextprotocol/adcp-client-python/commit/874d170145cea29a88d93bec320dccdef35ba748))


### Bug Fixes

* apply PR suggestion ([cecc2dc](https://github.com/adcontextprotocol/adcp-client-python/commit/cecc2dcc0ff926d1d9d978ed9ffce578f85186dc))
* drop --collapse-root-models option from type gen. Regenerate types ([6cba9ef](https://github.com/adcontextprotocol/adcp-client-python/commit/6cba9efc3b9cdb54835a0c2c35c6eed28efb0bb0))
* format ([aabb322](https://github.com/adcontextprotocol/adcp-client-python/commit/aabb322f98c5565d8d29f1e1a104f16b02656bda))
* format ([1dca6f1](https://github.com/adcontextprotocol/adcp-client-python/commit/1dca6f1bd26ee5046eb7594a5995da600b6d9394))
* format ([83da78d](https://github.com/adcontextprotocol/adcp-client-python/commit/83da78d752efeb27c163a52c383fc6039827bfec))
* handle all authorization types in get_properties_by_agent ([#100](https://github.com/adcontextprotocol/adcp-client-python/issues/100)) ([934f437](https://github.com/adcontextprotocol/adcp-client-python/commit/934f437c9ed260fbd748a2c74a3b4cd0c3bb0bc9))
* regenerate schemas ([4ab4f53](https://github.com/adcontextprotocol/adcp-client-python/commit/4ab4f53be009faf2c7e3e07312d97e0ceebfa7d9))
* tests ([e1664aa](https://github.com/adcontextprotocol/adcp-client-python/commit/e1664aa825b667aa7d0f0f9b3a6daae2a32ffd0e))
* update MCP SDK to &gt;=1.23.2 for streaming stability ([#94](https://github.com/adcontextprotocol/adcp-client-python/issues/94)) ([3c25822](https://github.com/adcontextprotocol/adcp-client-python/commit/3c258225e4e609e018b14dd9c32f9f1d6fcb7b68))
* update verify signature function to work with timestamp ([aaa7c8b](https://github.com/adcontextprotocol/adcp-client-python/commit/aaa7c8be915f890de3129268851ec32e3ec42e63))
* utility functions ([bc47233](https://github.com/adcontextprotocol/adcp-client-python/commit/bc47233e6465f9d23fd6a6c93f5c3af31b7b4173))

## [2.13.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.12.2...v2.13.0) (2025-12-07)


### Features

* add __str__ methods and filter params tests ([#92](https://github.com/adcontextprotocol/adcp-client-python/issues/92)) ([6358542](https://github.com/adcontextprotocol/adcp-client-python/commit/6358542df3db2bf465b7bb342c2c49be6d455a15))

## [2.12.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.12.1...v2.12.2) (2025-11-29)


### Bug Fixes

* handle BaseExceptionGroup with CancelledError during cleanup ([#89](https://github.com/adcontextprotocol/adcp-client-python/issues/89)) ([940e6ee](https://github.com/adcontextprotocol/adcp-client-python/commit/940e6eef57c9ffdfde2981fe8d3a36d66ddb76b2))
* handle ExceptionGroup and CancelledError in MCP error flow ([#87](https://github.com/adcontextprotocol/adcp-client-python/issues/87)) ([27ff0ae](https://github.com/adcontextprotocol/adcp-client-python/commit/27ff0ae9e2451540bbba7d7d0cbc4133c0c7d223))
* use official A2A SDK for spec-compliant client implementation ([#90](https://github.com/adcontextprotocol/adcp-client-python/issues/90)) ([d1b55cf](https://github.com/adcontextprotocol/adcp-client-python/commit/d1b55cfbb19675c286336d6a484efa797b8df1c9))

## [2.12.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.12.0...v2.12.1) (2025-11-24)


### Bug Fixes

* correct ADCP_VERSION packaging for PyPI ([#85](https://github.com/adcontextprotocol/adcp-client-python/issues/85)) ([161656d](https://github.com/adcontextprotocol/adcp-client-python/commit/161656d97af9651cd76332935a3421ddf07acf55))

## [2.12.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.11.1...v2.12.0) (2025-11-22)


### Features

* align A2A adapter with canonical response format ([#83](https://github.com/adcontextprotocol/adcp-client-python/issues/83)) ([f7062f2](https://github.com/adcontextprotocol/adcp-client-python/commit/f7062f2857c5a1cb32d40407766992b4ea272c23))

## [2.11.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.11.0...v2.11.1) (2025-11-21)


### Bug Fixes

* trigger release for PropertyTag/PropertyId schema updates ([#81](https://github.com/adcontextprotocol/adcp-client-python/issues/81)) ([f9556c5](https://github.com/adcontextprotocol/adcp-client-python/commit/f9556c5ab7319761844643175f0c588e717f1417))

## [2.11.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.10.0...v2.11.0) (2025-11-21)


### Features

* sync AdCP schema updates and simplify type architecture ([#78](https://github.com/adcontextprotocol/adcp-client-python/issues/78)) ([58e0d24](https://github.com/adcontextprotocol/adcp-client-python/commit/58e0d2441eac43858573f69c7d4f51e0dc8658bc))

## [2.10.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.9.0...v2.10.0) (2025-11-20)


### Features

* add create_media_buy, update_media_buy, and build_creative to SimpleAPI ([#74](https://github.com/adcontextprotocol/adcp-client-python/issues/74)) ([5545071](https://github.com/adcontextprotocol/adcp-client-python/commit/55450715ee1bfd6e01a886488c5d13b5f4da514e))

## [2.9.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.8.0...v2.9.0) (2025-11-20)


### Features

* add missing AdCP protocol methods to CLI and client ([#71](https://github.com/adcontextprotocol/adcp-client-python/issues/71)) ([f9432a3](https://github.com/adcontextprotocol/adcp-client-python/commit/f9432a34d49ac250e7fb3c2d626cd889c4ef0e6c))
* integrate AdCP schema improvements (PR [#222](https://github.com/adcontextprotocol/adcp-client-python/issues/222) + [#223](https://github.com/adcontextprotocol/adcp-client-python/issues/223)) ([#73](https://github.com/adcontextprotocol/adcp-client-python/issues/73)) ([d61ca33](https://github.com/adcontextprotocol/adcp-client-python/commit/d61ca33c68848ffa951e05b85c969cd6685f8fc6))

## [2.8.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.7.0...v2.8.0) (2025-11-19)


### Features

* expose all types through stable public API ([#69](https://github.com/adcontextprotocol/adcp-client-python/issues/69)) ([3abef14](https://github.com/adcontextprotocol/adcp-client-python/commit/3abef1489813b663bfa5c4b3ab7cca755dc63b53))

## [2.7.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.6.0...v2.7.0) (2025-11-19)


### Features

* export FormatId, PackageRequest, PushNotificationConfig, and PriceGuidance from stable API ([#68](https://github.com/adcontextprotocol/adcp-client-python/issues/68)) ([654f882](https://github.com/adcontextprotocol/adcp-client-python/commit/654f8826455bf67f5b9cfb5717c6cd1af84b648e))


### Bug Fixes

* trigger 2.6.1 release for import boundary enforcement ([#66](https://github.com/adcontextprotocol/adcp-client-python/issues/66)) ([ee8017a](https://github.com/adcontextprotocol/adcp-client-python/commit/ee8017a455e6fb4bdb8c0928214c41f33a59f529))

## [2.6.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.5.0...v2.6.0) (2025-11-19)


### Features

* complete semantic alias coverage for discriminated unions ([#63](https://github.com/adcontextprotocol/adcp-client-python/issues/63)) ([ab49c98](https://github.com/adcontextprotocol/adcp-client-python/commit/ab49c98b584cc75fccb7cf9b5810b5c883b16ce4))

## [2.5.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.4.1...v2.5.0) (2025-11-18)


### Features

* Add API reference documentation with pdoc3 ([#59](https://github.com/adcontextprotocol/adcp-client-python/issues/59)) ([6f7fdf1](https://github.com/adcontextprotocol/adcp-client-python/commit/6f7fdf164eb2e1f9c0b471392cf94b43296765fc))


### Bug Fixes

* resolve Package type name collision with semantic aliases ([#62](https://github.com/adcontextprotocol/adcp-client-python/issues/62)) ([b4028b7](https://github.com/adcontextprotocol/adcp-client-python/commit/b4028b714eebcb3bf035529fa1f8320e568df287))

## [2.4.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.4.0...v2.4.1) (2025-11-18)


### Bug Fixes

* remove stale generated files and improve type generation ([#58](https://github.com/adcontextprotocol/adcp-client-python/issues/58)) ([574ec90](https://github.com/adcontextprotocol/adcp-client-python/commit/574ec9080c5d79b39ad857f940ca666e3e67fa5e))

## [2.4.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.3.0...v2.4.0) (2025-11-18)


### Features

* Add is_fixed discriminator to pricing types ([#56](https://github.com/adcontextprotocol/adcp-client-python/issues/56)) ([e47ff66](https://github.com/adcontextprotocol/adcp-client-python/commit/e47ff66e0b1a58132fe8e940f4bfc8917642113d))

## [2.3.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.2.0...v2.3.0) (2025-11-17)


### Features

* Add publisher authorization discovery API ([#54](https://github.com/adcontextprotocol/adcp-client-python/issues/54)) ([e7d0696](https://github.com/adcontextprotocol/adcp-client-python/commit/e7d06963c7d9538177798d31b8cb60a9775312b7))


### Bug Fixes

* Move email-validator to runtime dependencies ([#51](https://github.com/adcontextprotocol/adcp-client-python/issues/51)) ([6ce1535](https://github.com/adcontextprotocol/adcp-client-python/commit/6ce15357dda673125a48335a3c30774865b617ba))

## [2.2.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.1.0...v2.2.0) (2025-11-16)


### Features

* Add semantic type aliases for discriminated unions ([#49](https://github.com/adcontextprotocol/adcp-client-python/issues/49)) ([d776bc6](https://github.com/adcontextprotocol/adcp-client-python/commit/d776bc65277f34f3cdf85b72ec95f9759fd006a3))

## [2.1.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v2.0.0...v2.1.0) (2025-11-16)


### Features

* Add ergonomic type aliases and public API exports ([#47](https://github.com/adcontextprotocol/adcp-client-python/issues/47)) ([5c63dec](https://github.com/adcontextprotocol/adcp-client-python/commit/5c63decdd12c9d565c1b0fced0b3ed4348e1bd75))

## [2.0.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.6.1...v2.0.0) (2025-11-15)


### ⚠ BREAKING CHANGES

*

### Features

* Migrate to datamodel-code-generator for type generation ([#45](https://github.com/adcontextprotocol/adcp-client-python/issues/45)) ([8844d69](https://github.com/adcontextprotocol/adcp-client-python/commit/8844d694e758c47a670fd9fcd8c1eeaaee7b6b29))

## [1.6.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.6.0...v1.6.1) (2025-11-13)


### Bug Fixes

* context field for custom injected types ([e37c095](https://github.com/adcontextprotocol/adcp-client-python/commit/e37c095ffcca8e8586aeea84a854721c64f990d1))
* context field for custom injected types ([8e96c6e](https://github.com/adcontextprotocol/adcp-client-python/commit/8e96c6e06782eab3c78effbfc2e16f40fd2f7466))
* mypy failures ([c186472](https://github.com/adcontextprotocol/adcp-client-python/commit/c186472304eb494dda95fd713993a289269a43f2))
* ruff check ([b8296e5](https://github.com/adcontextprotocol/adcp-client-python/commit/b8296e52da0c3d1d9f4cd524946a47519cfd802a))

## [1.6.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.5.0...v1.6.0) (2025-11-13)


### Features

* add adagents.json validation and discovery ([#42](https://github.com/adcontextprotocol/adcp-client-python/issues/42)) ([4ea16a1](https://github.com/adcontextprotocol/adcp-client-python/commit/4ea16a141a52aa1996420b9d8042d3f8d9d8ddd6))
* add AdCPBaseModel with exclude_none serialization ([#40](https://github.com/adcontextprotocol/adcp-client-python/issues/40)) ([c3cd590](https://github.com/adcontextprotocol/adcp-client-python/commit/c3cd5902d1ea9ad62e3e61b6e843b64e768feead))

## [1.5.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.4.1...v1.5.0) (2025-11-13)


### Features

* generate type adcp context field ([38600f2](https://github.com/adcontextprotocol/adcp-client-python/commit/38600f2f10a922efb3d9e05b81e37bafe219baa9))
* generate type adcp context field ([92e90c9](https://github.com/adcontextprotocol/adcp-client-python/commit/92e90c96f5d757d119cc6e3ddb7f77078f8e979c))

## [1.4.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.4.0...v1.4.1) (2025-11-11)


### Bug Fixes

* handle MCP error responses without structuredContent ([#34](https://github.com/adcontextprotocol/adcp-client-python/issues/34)) ([52956bc](https://github.com/adcontextprotocol/adcp-client-python/commit/52956bccb86c649024c4b111a890ac43e30d321b))

## [1.4.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.3.1...v1.4.0) (2025-11-10)


### Features

* add ergonomic .simple accessor to all ADCPClient instances ([#32](https://github.com/adcontextprotocol/adcp-client-python/issues/32)) ([5404325](https://github.com/adcontextprotocol/adcp-client-python/commit/54043251e86b44264070c37025d5bd9407bb906b))

## [1.3.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v1.3.0...v1.3.1) (2025-11-10)


### Bug Fixes

* export no-auth test helpers from main module ([#30](https://github.com/adcontextprotocol/adcp-client-python/issues/30)) ([fb2459d](https://github.com/adcontextprotocol/adcp-client-python/commit/fb2459da396ee1dfd01bfa437130b0042f8360e7))

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
