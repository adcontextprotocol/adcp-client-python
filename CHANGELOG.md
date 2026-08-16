# Changelog

## [8.0.0-rc.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v7.0.0-rc.2...v8.0.0-rc.2) (2026-08-16)


### ⚠ BREAKING CHANGES

* **identity:** create_roster_account_store now requires an authorize callback.

### Bug Fixes

* **identity:** enforce tenant and credential isolation ([#1001](https://github.com/adcontextprotocol/adcp-client-python/issues/1001)) ([95a3a6e](https://github.com/adcontextprotocol/adcp-client-python/commit/95a3a6e0540bd89ce6a34c055072ca96ef21513e))
* **release:** harden version 7 for stable release ([6fca33c](https://github.com/adcontextprotocol/adcp-client-python/commit/6fca33c9f13af41b1b3b698792e54437fc52e445))

## [7.0.0-rc.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v7.0.0-rc.1...v7.0.0-rc.2) (2026-08-13)


### Features

* **protocol:** support AdCP 3.1.13 schemas ([#1021](https://github.com/adcontextprotocol/adcp-client-python/issues/1021)) ([71a45b4](https://github.com/adcontextprotocol/adcp-client-python/commit/71a45b48442d5ab72d1190f44039595ba548d992))


### Bug Fixes

* **ci:** harden privileged workflows ([#1003](https://github.com/adcontextprotocol/adcp-client-python/issues/1003)) ([688b3e4](https://github.com/adcontextprotocol/adcp-client-python/commit/688b3e4c6f4551f7f04d4ce68f6a69a5ee943523))
* **decisioning:** disable sync task webhooks by default ([#1020](https://github.com/adcontextprotocol/adcp-client-python/issues/1020)) ([46c464a](https://github.com/adcontextprotocol/adcp-client-python/commit/46c464a33780bbce2424438cab99e5cf900df899))
* **security:** harden external sinks and callbacks ([#1002](https://github.com/adcontextprotocol/adcp-client-python/issues/1002)) ([e27c6a7](https://github.com/adcontextprotocol/adcp-client-python/commit/e27c6a7a8b9831806540f69101f2ad4e33c3a162))

## [7.0.0-rc.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v7.0.0-rc...v7.0.0-rc.1) (2026-08-05)


### Features

* **protocol:** support AdCP 3.1.10 schemas ([#1012](https://github.com/adcontextprotocol/adcp-client-python/issues/1012)) ([dbf3df8](https://github.com/adcontextprotocol/adcp-client-python/commit/dbf3df881d46421e494171b64cf4a5cf10a9103c))


### Migration Notes

* **Experimental Trusted Match/TMPX:** AdCP 3.1.10 replaces provider-supplied `tmpx_macros`/`macros` with publisher-owned `tmpx_slots`/`chunks`. `IdentityMatchResponse` now models the router-to-publisher response shape; its import alias remains available, but old-shape payloads no longer validate.


### Bug Fixes

* **client:** preserve canonical format options ([#1006](https://github.com/adcontextprotocol/adcp-client-python/issues/1006)) ([96396b9](https://github.com/adcontextprotocol/adcp-client-python/commit/96396b93bdd6680c2ba06207ad18f61de744620c))
* **release:** compare normalized prerelease versions ([#1015](https://github.com/adcontextprotocol/adcp-client-python/issues/1015)) ([87140b4](https://github.com/adcontextprotocol/adcp-client-python/commit/87140b4aa76b67d26b57a5abb13d21dad06e0b47))

## [7.0.0-rc](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.6.0...v7.0.0-rc) (2026-07-29)


### ⚠ BREAKING CHANGES

* **creative:** FormatId is removed from the normal root API in favor of LegacyFormatId and explicit legacy modules. BuildCreativeRequest/Response and PreviewCreativeRequest/Response variants are renamed to Legacy*; ADCPClient build_creative/preview_creative and list_creative_formats move to *_legacy methods. Product and creative filters no longer expose format_ids, and generic primary execution rejects legacy-only creative tasks.

### Features

* **creative:** make canonical models primary in v7 ([#994](https://github.com/adcontextprotocol/adcp-client-python/issues/994)) ([b5c6f16](https://github.com/adcontextprotocol/adcp-client-python/commit/b5c6f1644cb5ecbd9377145051092dab69e1741b))
* **protocol:** support AdCP 3.1.8 schemas ([#992](https://github.com/adcontextprotocol/adcp-client-python/issues/992)) ([0efb159](https://github.com/adcontextprotocol/adcp-client-python/commit/0efb1590dd5efaee4f2659a46a5a839b442502c8))


### Bug Fixes

* **security:** harden SDK auth transports ([a2610a5](https://github.com/adcontextprotocol/adcp-client-python/commit/a2610a5b4f8e0d0d1d500fc3908be1d6862b0764))
* **server:** unify divergent host normalizers behind one helper ([#997](https://github.com/adcontextprotocol/adcp-client-python/issues/997)) ([6fb1b72](https://github.com/adcontextprotocol/adcp-client-python/commit/6fb1b72d45ec8adfc2be93c5232464d6ff1c69e3))
* **signing:** block CGNAT and 6to4 relay ranges in SSRF validation ([#974](https://github.com/adcontextprotocol/adcp-client-python/issues/974)) ([0207429](https://github.com/adcontextprotocol/adcp-client-python/commit/020742979cabb5e721ab33c41caddb6b9849074a))
* **signing:** canonicalize both origins in the brand.json jwks gate ([#998](https://github.com/adcontextprotocol/adcp-client-python/issues/998)) ([b7ef1df](https://github.com/adcontextprotocol/adcp-client-python/commit/b7ef1dfcff2bf116e1c18586fd26a497669cdc41))
* **signing:** idna-normalize hosts in etld+1 binding ([#995](https://github.com/adcontextprotocol/adcp-client-python/issues/995)) ([827e4f4](https://github.com/adcontextprotocol/adcp-client-python/commit/827e4f4e9c259137e92124b0f309233685c43d51)), closes [#988](https://github.com/adcontextprotocol/adcp-client-python/issues/988)
* **signing:** preserve a trailing empty query in [@target-uri](https://github.com/target-uri) ([#987](https://github.com/adcontextprotocol/adcp-client-python/issues/987)) ([afa0454](https://github.com/adcontextprotocol/adcp-client-python/commit/afa0454598320a118f8753b79737f0a320b7afe2))
* **signing:** reject malformed authorities, convert IDN hosts to A-labels ([#985](https://github.com/adcontextprotocol/adcp-client-python/issues/985)) ([be233e4](https://github.com/adcontextprotocol/adcp-client-python/commit/be233e4b514f6cd34d3aadd3311675821b2e8290))
* **signing:** reject malformed structured-field input at step 1 ([#993](https://github.com/adcontextprotocol/adcp-client-python/issues/993)) ([6518c2d](https://github.com/adcontextprotocol/adcp-client-python/commit/6518c2d88d0bd5055caeb39350a24a3b34c45abc))
* **webhooks:** bracket and structurally validate DockerLocalhostRewrite rewrite_to ([#996](https://github.com/adcontextprotocol/adcp-client-python/issues/996)) ([013c06c](https://github.com/adcontextprotocol/adcp-client-python/commit/013c06cf30aa62046a2ce2f316057f577c214f1a))

## [6.6.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.5.0...v6.6.0) (2026-07-01)


### Features

* **protocol:** support AdCP 3.1.1 schemas ([22ae047](https://github.com/adcontextprotocol/adcp-client-python/commit/22ae0472a22f8e0111456d8e0660d242c77bff23))


### Bug Fixes

* **adagents:** follow safe well-known redirects ([fbb91c1](https://github.com/adcontextprotocol/adcp-client-python/commit/fbb91c113bfeb003f0c182ae0f6457fc65df07ba))

## [6.5.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.4.1...v6.5.0) (2026-06-28)


### Features

* **types:** lazy, typed public surface with curated partial modules ([#963](https://github.com/adcontextprotocol/adcp-client-python/issues/963)) ([cad9840](https://github.com/adcontextprotocol/adcp-client-python/commit/cad9840e2662ee063f0df385ada07e289a2b2cf7))

## [6.4.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.4.0...v6.4.1) (2026-06-28)


### Performance Improvements

* **types:** defer pydantic core-schema builds to cut import memory ([#959](https://github.com/adcontextprotocol/adcp-client-python/issues/959)) ([f418961](https://github.com/adcontextprotocol/adcp-client-python/commit/f4189611740fdc4eaf4e7e514a16e6f17d07598e))

## [6.4.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.1...v6.4.0) (2026-06-20)


### Features

* **server/auth:** allow_unauthenticated for network-trust deployments ([f41764e](https://github.com/adcontextprotocol/adcp-client-python/commit/f41764e2a25bc77a268c2b6519f49b64aa1e8de3))

## [6.3.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.0...v6.3.1) (2026-06-18)


### Bug Fixes

* **packaging:** trim prerelease schema bundles ([2db8bd1](https://github.com/adcontextprotocol/adcp-client-python/commit/2db8bd12461136d280c23906bb34b3d8c9cbd5d9))

## [6.3.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.0-beta.9...v6.3.0) (2026-06-18)


### Features

* **protocol:** support AdCP 3.1.0-rc.13
* **server:** project native postal capabilities to legacy booleans for AdCP 3.0 callers


### Bug Fixes

* **protocol:** pin AdCP 3.1 stable
* **schemas:** stamp primary latest fallback caches with the requested prerelease version

## [6.3.0-beta.9](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.0-beta.8...v6.3.0-beta.9) (2026-06-07)


### ⚠ BREAKING CHANGES

* RegistryClient.create_adagents now returns a CreateAdagentsResponse Pydantic model instead of dict[str, Any]. Callers using subscript access (result["success"], result["data"]) must switch to attribute access (result.success, result.data). The model matches the /api/adagents/create wire contract (adcp#5385).

### Features

* add FeedMirror client-side wholesale feed mirror ([#940](https://github.com/adcontextprotocol/adcp-client-python/issues/940)) ([64ce9ba](https://github.com/adcontextprotocol/adcp-client-python/commit/64ce9bae48d5c01f43d9f40bb6b15c256cbd4ce5))
* **codegen:** fail consolidate on unallowlisted name collisions ([#919](https://github.com/adcontextprotocol/adcp-client-python/issues/919)) ([d9367a0](https://github.com/adcontextprotocol/adcp-client-python/commit/d9367a0b20d2884146f207b13b11e3a0820911a8))
* **decisioning:** async (handoff) discovery for get_products / get_signals ([#930](https://github.com/adcontextprotocol/adcp-client-python/issues/930)) ([76fa4fa](https://github.com/adcontextprotocol/adcp-client-python/commit/76fa4fa26d10ffdd22aa10526e292f7a84aa8632))
* **protocol:** support AdCP 3.1.0-rc.10 ([#944](https://github.com/adcontextprotocol/adcp-client-python/issues/944)) ([65e4368](https://github.com/adcontextprotocol/adcp-client-python/commit/65e4368fa5536248e64dd554e3bae452a9ef103a))
* **registry:** community-mirror lifecycle + upsert client (JS [#2183](https://github.com/adcontextprotocol/adcp-client-python/issues/2183)/[#2187](https://github.com/adcontextprotocol/adcp-client-python/issues/2187) parity) ([#929](https://github.com/adcontextprotocol/adcp-client-python/issues/929)) ([11e5cda](https://github.com/adcontextprotocol/adcp-client-python/commit/11e5cda9821bc6ce3b99d031b661121caaaa2e37))
* **server:** lazy idempotency backend wrapper (JS [#2136](https://github.com/adcontextprotocol/adcp-client-python/issues/2136) parity) ([#928](https://github.com/adcontextprotocol/adcp-client-python/issues/928)) ([1ee6f5b](https://github.com/adcontextprotocol/adcp-client-python/commit/1ee6f5b2613766160fb87a3fbc7b184bbb82ee8a))
* **server:** response_enhancer callback (JS [#2161](https://github.com/adcontextprotocol/adcp-client-python/issues/2161) parity) ([#933](https://github.com/adcontextprotocol/adcp-client-python/issues/933)) ([659cd7c](https://github.com/adcontextprotocol/adcp-client-python/commit/659cd7cb525db1a1aa5a1556eccdcea91baecc42))
* **testing:** reference UpstreamRecorder middleware for query_upstream_traffic ([#939](https://github.com/adcontextprotocol/adcp-client-python/issues/939)) ([5ca5758](https://github.com/adcontextprotocol/adcp-client-python/commit/5ca5758faecce74e84d948b18c94732319a687c1))
* type RegistryClient.create_adagents return as CreateAdagentsResponse ([#937](https://github.com/adcontextprotocol/adcp-client-python/issues/937)) ([8e6018b](https://github.com/adcontextprotocol/adcp-client-python/commit/8e6018baa66f129696a6c80875c42b12c113de98))
* **types:** disambiguated aliases for high-traffic colliding type names ([#941](https://github.com/adcontextprotocol/adcp-client-python/issues/941)) ([6c058f0](https://github.com/adcontextprotocol/adcp-client-python/commit/6c058f03f8bbbd791774d9599d3713883b643a51))


### Bug Fixes

* **adagents:** pin DNS to close rebinding TOCTOU on SSRF gate ([#920](https://github.com/adcontextprotocol/adcp-client-python/issues/920)) ([51de333](https://github.com/adcontextprotocol/adcp-client-python/commit/51de3336dc0a6a79d3dbfbfc51462a9c67ffc266)), closes [#757](https://github.com/adcontextprotocol/adcp-client-python/issues/757)
* **codegen:** rewrite compact ConfigDict extra in place ([#935](https://github.com/adcontextprotocol/adcp-client-python/issues/935)) ([4d3303f](https://github.com/adcontextprotocol/adcp-client-python/commit/4d3303f6e51f77fead2b932d967febeb6708c7a8))
* **decisioning:** gate async-completion webhook on SPEC_WEBHOOK_TASK_TYPES ([#932](https://github.com/adcontextprotocol/adcp-client-python/issues/932)) ([c23b406](https://github.com/adcontextprotocol/adcp-client-python/commit/c23b406493870a671322d5d3656ff18f71f4fee9)), closes [#931](https://github.com/adcontextprotocol/adcp-client-python/issues/931)
* **server:** centralize pre-validation hooks and attribute chain failures ([#938](https://github.com/adcontextprotocol/adcp-client-python/issues/938)) ([3859131](https://github.com/adcontextprotocol/adcp-client-python/commit/3859131159b7e2bedc2ead3dc1dcd7dbc766ac9a)), closes [#859](https://github.com/adcontextprotocol/adcp-client-python/issues/859) [#882](https://github.com/adcontextprotocol/adcp-client-python/issues/882)
* **types:** derive response arms from schemas ([#922](https://github.com/adcontextprotocol/adcp-client-python/issues/922)) ([70abb5a](https://github.com/adcontextprotocol/adcp-client-python/commit/70abb5aca884a5af3b9339b3d9da73832bf22034))

## [6.3.0-beta.8](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.0-beta.7...v6.3.0-beta.8) (2026-06-05)


### ⚠ BREAKING CHANGES

* **server:** Debug endpoints now fail closed when enable_debug_endpoints=True unless callers pass debug_validate_request= or explicitly set debug_public=True for local/test deployments.

### Features

* **protocol:** support AdCP 3.1.0-rc.8 ([#912](https://github.com/adcontextprotocol/adcp-client-python/issues/912)) ([9782830](https://github.com/adcontextprotocol/adcp-client-python/commit/9782830499d2ac8dbec62a4070b866bfc173b804))
* **registry:** update rc9 capabilities ([#916](https://github.com/adcontextprotocol/adcp-client-python/issues/916)) ([432e2a5](https://github.com/adcontextprotocol/adcp-client-python/commit/432e2a5152fc96e3d36a35e3c7f758f72a2fec4b))
* **server:** harden MCP session lifecycle ([#908](https://github.com/adcontextprotocol/adcp-client-python/issues/908)) ([caa2735](https://github.com/adcontextprotocol/adcp-client-python/commit/caa273531422f20298192f5dd7e33f36b8e02db4))


### Bug Fixes

* **types:** generate schema enums as StrEnum ([#915](https://github.com/adcontextprotocol/adcp-client-python/issues/915)) ([6ad4fe4](https://github.com/adcontextprotocol/adcp-client-python/commit/6ad4fe4e7db413b4be15c797d34d5d9670abdc7e))

## [6.3.0-beta.7](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.0-beta.6...v6.3.0-beta.7) (2026-05-30)

### Features

* **client:** add explicit MCP Streamable HTTP session termination helper ([#903](https://github.com/adcontextprotocol/adcp-client-python/issues/903))
* **server:** expose opt-in MCP session debug snapshots ([#903](https://github.com/adcontextprotocol/adcp-client-python/issues/903))

### Breaking changes

* **server:** `enable_debug_endpoints=True` now fails closed unless callers also pass `debug_validate_request=` or explicitly opt into unauthenticated local/test access with `debug_public=True`.

### Security

* **server:** debug endpoints are no longer exposed unauthenticated by default; use a validator such as an `X-Debug-Token` check for network-reachable deployments.

### Bug Fixes

* **server:** guard stateful MCP sessions ([#904](https://github.com/adcontextprotocol/adcp-client-python/issues/904)) ([cb11a95](https://github.com/adcontextprotocol/adcp-client-python/commit/cb11a95487c067d9a844f2e954de95dfab7d55d5))
* **types:** update AdCP rc4 schemas ([71c45e5](https://github.com/adcontextprotocol/adcp-client-python/commit/71c45e588d9626962f45e0e303cff665f2780727))

## [6.3.0-beta.6](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.0-beta.5...v6.3.0-beta.6) (2026-05-28)


### Bug Fixes

* **validation:** bundle schemas before wheel builds ([0cdcf29](https://github.com/adcontextprotocol/adcp-client-python/commit/0cdcf29b6902961b9b81150049fda9eb3628130e))

## [6.3.0-beta.5](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.3.0-beta.4...v6.3.0-beta.5) (2026-05-28)


### Features

* add creative format compatibility helpers ([#887](https://github.com/adcontextprotocol/adcp-client-python/issues/887)) ([7f26ad1](https://github.com/adcontextprotocol/adcp-client-python/commit/7f26ad19d6bcef0140feeecfe8244d5975202821))
* **canonical-formats:** add canonical reference resolver ([#891](https://github.com/adcontextprotocol/adcp-client-python/issues/891)) ([d5706cd](https://github.com/adcontextprotocol/adcp-client-python/commit/d5706cdbc184a48f58a8a3a6ae89f4f435b26bd1))


### Bug Fixes

* **ci:** block agent PR title prefixes ([#893](https://github.com/adcontextprotocol/adcp-client-python/issues/893)) ([79a979d](https://github.com/adcontextprotocol/adcp-client-python/commit/79a979dd2fc27c351c9c5d08a63fefa4cd372be6))
* **types:** support AdCP 3.1 beta 7 ([3eee8d5](https://github.com/adcontextprotocol/adcp-client-python/commit/3eee8d52101f5fe062c0d8de9f8fd9f26c4e5d51))

## [6.3.0-beta.4](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.2.0-beta.4...v6.3.0-beta.4) (2026-05-26)


### Features

* allow externally managed webhook signing capabilities ([#876](https://github.com/adcontextprotocol/adcp-client-python/issues/876)) ([3c312a0](https://github.com/adcontextprotocol/adcp-client-python/commit/3c312a0dacea1c99535dac0bac68db54a6301d26))

## [6.2.0-beta.4](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.2.0-beta.3...v6.2.0-beta.4) (2026-05-26)


### Features

* **decisioning:** add request-scoped capabilities hook ([#869](https://github.com/adcontextprotocol/adcp-client-python/issues/869)) ([23fb317](https://github.com/adcontextprotocol/adcp-client-python/commit/23fb3176a6d88f6986f5c81f185bc2fd848f822d))
* **server:** negotiate media buy version handling and update actions ([#871](https://github.com/adcontextprotocol/adcp-client-python/issues/871)) ([5a89d86](https://github.com/adcontextprotocol/adcp-client-python/commit/5a89d86ffaf006ce79352eeb05365517b6f7791c))
* support AdCP 3.1 Beta 4 ([#872](https://github.com/adcontextprotocol/adcp-client-python/issues/872)) ([1bae6e8](https://github.com/adcontextprotocol/adcp-client-python/commit/1bae6e895a125080158b06f758803ed17a071a25))

## [6.2.0-beta.3](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.1.1-beta.2...v6.2.0-beta.3) (2026-05-26)


### Features

* **adagents:** add permissive property resolver ([#863](https://github.com/adcontextprotocol/adcp-client-python/issues/863)) ([7f45742](https://github.com/adcontextprotocol/adcp-client-python/commit/7f457422beec2912e10a84f7e3eed665bbc458af))
* **decisioning:** add full stack registry factory ([#862](https://github.com/adcontextprotocol/adcp-client-python/issues/862)) ([28c4f64](https://github.com/adcontextprotocol/adcp-client-python/commit/28c4f6490a89fb9af413e6d3df7328878f60d94c))
* **decisioning:** add registry observer removal ([#861](https://github.com/adcontextprotocol/adcp-client-python/issues/861)) ([2657a8b](https://github.com/adcontextprotocol/adcp-client-python/commit/2657a8ba0c72556aea027bb5a32a3897d299944f))
* **handlers:** expose sync_catalogs through sales-catalog-driven specialism ([#838](https://github.com/adcontextprotocol/adcp-client-python/issues/838)) ([6fdcb77](https://github.com/adcontextprotocol/adcp-client-python/commit/6fdcb7758e6d7123ffc9493c9a1c89ea24a3fc3d))
* **server:** add unknown-field policy and hook composition ([#860](https://github.com/adcontextprotocol/adcp-client-python/issues/860)) ([7b830c3](https://github.com/adcontextprotocol/adcp-client-python/commit/7b830c3ccd124729e28471ec753243cc10d8cc55))


### Bug Fixes

* **adagents:** raise AdagentsAccessBlockedError on 403 + cf-mitigated: challenge ([#837](https://github.com/adcontextprotocol/adcp-client-python/issues/837)) ([bc4111f](https://github.com/adcontextprotocol/adcp-client-python/commit/bc4111f7f26b63a08b5d2e324abbbc7a39b6dbe1))
* **decisioning:** project malformed-JSON 2xx responses to AdcpError ([#836](https://github.com/adcontextprotocol/adcp-client-python/issues/836)) ([c547dfe](https://github.com/adcontextprotocol/adcp-client-python/commit/c547dfe66148d393f705443f8a4c2dac50f6e749)), closes [#453](https://github.com/adcontextprotocol/adcp-client-python/issues/453)
* expose requested AdCP version resolver ([#855](https://github.com/adcontextprotocol/adcp-client-python/issues/855)) ([f1d88bc](https://github.com/adcontextprotocol/adcp-client-python/commit/f1d88bc8746beb372f8155f494942006291f8d78))
* **types:** add update media buy response coercion ([#864](https://github.com/adcontextprotocol/adcp-client-python/issues/864)) ([c111ba6](https://github.com/adcontextprotocol/adcp-client-python/commit/c111ba6a04467c06eb8d8a57d814bdb0b91d93da))
* **webhooks:** promote McpWebhookPayload.token from extras shim to typed field ([#835](https://github.com/adcontextprotocol/adcp-client-python/issues/835)) ([3832433](https://github.com/adcontextprotocol/adcp-client-python/commit/383243357e30c9fdb0b67a69b354676ace38efe7)), closes [#638](https://github.com/adcontextprotocol/adcp-client-python/issues/638)

## [6.1.1-beta.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.1.0-beta.2...v6.1.1-beta.2) (2026-05-25)


### Bug Fixes

* **codegen:** preserve regenerated response aliases ([#854](https://github.com/adcontextprotocol/adcp-client-python/issues/854)) ([56d65fd](https://github.com/adcontextprotocol/adcp-client-python/commit/56d65fd1187a90ed9128a691a359e862eea00e13))
* seller SDK version compatibility ([#852](https://github.com/adcontextprotocol/adcp-client-python/issues/852)) ([e4b716d](https://github.com/adcontextprotocol/adcp-client-python/commit/e4b716d05462eabf49262222376e8ad7d95198c1))

## [6.1.0-beta.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.1.0-beta.1...v6.1.0-beta.2) (2026-05-24)


### ⚠ BREAKING CHANGES

* read `media_buy_status` for lifecycle; `status` is now the task envelope.

### Features

* add webhook proof-of-control helper ([#843](https://github.com/adcontextprotocol/adcp-client-python/issues/843)) ([232f7ef](https://github.com/adcontextprotocol/adcp-client-python/commit/232f7ef6d08e16234b6227ac34f5ba091043a51e))
* align media-buy responses with AdCP 3.1 ([391dae6](https://github.com/adcontextprotocol/adcp-client-python/commit/391dae6c9c8a753a790aa049970c80205a14309d))
* **canonical-formats:** public API + v2→v1 projection ([#741](https://github.com/adcontextprotocol/adcp-client-python/issues/741), half 1 of 2) ([#841](https://github.com/adcontextprotocol/adcp-client-python/issues/841)) ([b455e2a](https://github.com/adcontextprotocol/adcp-client-python/commit/b455e2a8dd8197499404d546dfc512da1814e7c4))
* **canonical-formats:** v1↔v2 reverse + pixel_tracker + narrowing ([#741](https://github.com/adcontextprotocol/adcp-client-python/issues/741), half 2 of 2) ([#845](https://github.com/adcontextprotocol/adcp-client-python/issues/845)) ([37e9225](https://github.com/adcontextprotocol/adcp-client-python/commit/37e92256891892c923e303acf73a5a9fee904090))
* **examples:** dual-emit v1 format_ids + v2 format_options on reference seller ([#847](https://github.com/adcontextprotocol/adcp-client-python/issues/847)) ([7731f53](https://github.com/adcontextprotocol/adcp-client-python/commit/7731f53dc0ec82d60938b8467b16cce63f91e1c1)), closes [#741](https://github.com/adcontextprotocol/adcp-client-python/issues/741)


### Bug Fixes

* harden media buy response normalization ([5defa15](https://github.com/adcontextprotocol/adcp-client-python/commit/5defa15a51ad12cb89b0efbded726dbc2568bf40))
* harden media buy response normalization ([532459a](https://github.com/adcontextprotocol/adcp-client-python/commit/532459aed2fb4e5da9be58bdee43585529e5dbbf))

## [6.1.0-beta.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v6.0.0-beta.1...v6.1.0-beta.1) (2026-05-23)


### Features

* add webhook URL policy and wholesale feed sender ([65f84d2](https://github.com/adcontextprotocol/adcp-client-python/commit/65f84d2ea74775c08b067bd393ab82127c558918))

## [6.0.0-beta.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.7.0...v6.0.0-beta.1) (2026-05-23)


### Features

* **testing:** make SellerA2AClient event drain configurable ([#829](https://github.com/adcontextprotocol/adcp-client-python/issues/829)) ([62515d8](https://github.com/adcontextprotocol/adcp-client-python/commit/62515d85fe3753dcbfde8737c33f42a4a7829bb1))


### Bug Fixes

* send AAO directory pagination cursor as cursor ([#811](https://github.com/adcontextprotocol/adcp-client-python/issues/811)) ([da91183](https://github.com/adcontextprotocol/adcp-client-python/commit/da9118338856e78d00f08d12b0fd3178d819cfe5))


### Documentation

* add MIGRATION_v5_to_v6 stub for the 6.0-beta line ([#823](https://github.com/adcontextprotocol/adcp-client-python/issues/823)) ([c3837af](https://github.com/adcontextprotocol/adcp-client-python/commit/c3837af96f58dc37c22dbef47a02a2bbcaad3bf2)), closes [#802](https://github.com/adcontextprotocol/adcp-client-python/issues/802)
* **types:** refresh import-architecture references (closes [#807](https://github.com/adcontextprotocol/adcp-client-python/issues/807)) ([#828](https://github.com/adcontextprotocol/adcp-client-python/issues/828)) ([169bc80](https://github.com/adcontextprotocol/adcp-client-python/commit/169bc80d435c5f441960a0fc03687b6a126abd2d))

## [5.7.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.6.0...v5.7.0) (2026-05-22)


### Features

* **adagents:** divergence detector + ?include=properties for directory inverse-lookup ([#749](https://github.com/adcontextprotocol/adcp-client-python/issues/749) Part 3, adcp[#4894](https://github.com/adcontextprotocol/adcp-client-python/issues/4894)) ([#752](https://github.com/adcontextprotocol/adcp-client-python/issues/752)) ([1281076](https://github.com/adcontextprotocol/adcp-client-python/commit/128107638aaea1927e99db3fe67614d9e951f8c8))
* **adagents:** fetch_agent_authorizations_from_directory for AAO inverse lookup ([#769](https://github.com/adcontextprotocol/adcp-client-python/issues/769)) ([1c4e57d](https://github.com/adcontextprotocol/adcp-client-python/commit/1c4e57d47ee774b0be8f77540df1b3b60953a175))
* **adagents:** publisher_domains compact form, revoked_publisher_domains, streaming fetch caps (closes [#729](https://github.com/adcontextprotocol/adcp-client-python/issues/729)) ([#753](https://github.com/adcontextprotocol/adcp-client-python/issues/753)) ([352d1bb](https://github.com/adcontextprotocol/adcp-client-python/commit/352d1bb6719cfd20bf4a703d1d6bd0d1077a0c47))
* **decisioning:** emit AGENT_SUSPENDED / AGENT_BLOCKED dedicated codes (closes [#409](https://github.com/adcontextprotocol/adcp-client-python/issues/409)) ([#748](https://github.com/adcontextprotocol/adcp-client-python/issues/748)) ([12f8ffe](https://github.com/adcontextprotocol/adcp-client-python/commit/12f8ffe7fa4318affeb2ad728296db4ac88a452b))
* **decisioning:** Tier 3 brand-authz dispatch gate ([#350](https://github.com/adcontextprotocol/adcp-client-python/issues/350) stage 5, closes [#350](https://github.com/adcontextprotocol/adcp-client-python/issues/350)) ([#785](https://github.com/adcontextprotocol/adcp-client-python/issues/785)) ([1ad6df9](https://github.com/adcontextprotocol/adcp-client-python/commit/1ad6df907c5b81c98f079900cb8adf394bc6cbb1))
* **decisioning:** wire create_media_buy_store into PlatformHandler dispatch (closes [#462](https://github.com/adcontextprotocol/adcp-client-python/issues/462)) ([#773](https://github.com/adcontextprotocol/adcp-client-python/issues/773)) ([062c6f0](https://github.com/adcontextprotocol/adcp-client-python/commit/062c6f03e34dd47f818251c9b0382d3a7537b5dc))
* **examples:** handle @adcp/sdk@7.10.x storyboard scenarios (closes [#782](https://github.com/adcontextprotocol/adcp-client-python/issues/782)) ([#788](https://github.com/adcontextprotocol/adcp-client-python/issues/788)) ([7ef1b1a](https://github.com/adcontextprotocol/adcp-client-python/commit/7ef1b1a520b0059f6c8759f33fdb487cf727d36d))
* pick up beta 3 wholesale products and signals ([#799](https://github.com/adcontextprotocol/adcp-client-python/issues/799)) ([0deeaeb](https://github.com/adcontextprotocol/adcp-client-python/commit/0deeaebfd7269d9100185e9d842dedb071f3a991))
* **preview:** opt-in v3.1.0-beta.1 client surface ([#4761](https://github.com/adcontextprotocol/adcp-client-python/issues/4761), [#4762](https://github.com/adcontextprotocol/adcp-client-python/issues/4762), [#4763](https://github.com/adcontextprotocol/adcp-client-python/issues/4763)) ([#747](https://github.com/adcontextprotocol/adcp-client-python/issues/747)) ([35fdd47](https://github.com/adcontextprotocol/adcp-client-python/commit/35fdd47f9a3319b4325a8dc3597ab6a1a4dfc74f))
* **schemas:** patches/ post-process infra + restore [#753](https://github.com/adcontextprotocol/adcp-client-python/issues/753)/[#792](https://github.com/adcontextprotocol/adcp-client-python/issues/792) hand-edits ([#795](https://github.com/adcontextprotocol/adcp-client-python/issues/795)) ([4e6a722](https://github.com/adcontextprotocol/adcp-client-python/commit/4e6a722a70820fbdad50f38b0e15dc6b646daddb))
* **signing:** BrandAuthorizationResolver (Tier 3, [#350](https://github.com/adcontextprotocol/adcp-client-python/issues/350) stages 1-3) ([#770](https://github.com/adcontextprotocol/adcp-client-python/issues/770)) ([9028a88](https://github.com/adcontextprotocol/adcp-client-python/commit/9028a880adbd1b16b5fbc67f8b0c58f124f0add1))
* **signing:** verifier key_origins check + IDNA-2008 canonicalization (closes [#776](https://github.com/adcontextprotocol/adcp-client-python/issues/776), [#777](https://github.com/adcontextprotocol/adcp-client-python/issues/777)) ([#789](https://github.com/adcontextprotocol/adcp-client-python/issues/789)) ([31354b4](https://github.com/adcontextprotocol/adcp-client-python/commit/31354b4d813116d55e255c2b5f28fa599a09b0e7))
* **translation:** list known keys in TranslationMap KeyError ([#768](https://github.com/adcontextprotocol/adcp-client-python/issues/768)) ([7545424](https://github.com/adcontextprotocol/adcp-client-python/commit/75454245f1752d232fa7e52a38c8f201e9cbd3fe))
* **types:** auto-enforce publisher-selector XOR at Pydantic parse time (closes [#759](https://github.com/adcontextprotocol/adcp-client-python/issues/759)) ([#761](https://github.com/adcontextprotocol/adcp-client-python/issues/761)) ([e973864](https://github.com/adcontextprotocol/adcp-client-python/commit/e9738644b2016f57ebd55345fc7cc06775aa5a03))
* **validation:** validate_publisher_properties_item accepts Pydantic models ([#756](https://github.com/adcontextprotocol/adcp-client-python/issues/756)) ([01c6491](https://github.com/adcontextprotocol/adcp-client-python/commit/01c649119b3af5b8d39ce2e018559a01a6e96540))


### Bug Fixes

* **adagents:** disable follow_redirects on ads.txt MANAGERDOMAIN fetch ([#754](https://github.com/adcontextprotocol/adcp-client-python/issues/754)) ([76a679f](https://github.com/adcontextprotocol/adcp-client-python/commit/76a679fdf2f9aa7fd3d1ccfe36340e9750a95d15))
* **adagents:** inline-resolution path for publisher_properties selectors + publisher_domains[] fan-out ([#750](https://github.com/adcontextprotocol/adcp-client-python/issues/750)) ([659135f](https://github.com/adcontextprotocol/adcp-client-python/commit/659135f4c18af31d74bf2c7ef90cdb610b741011))
* **adagents:** resolve-and-validate gate against DNS-based SSRF ([#757](https://github.com/adcontextprotocol/adcp-client-python/issues/757), partial) ([#760](https://github.com/adcontextprotocol/adcp-client-python/issues/760)) ([969f373](https://github.com/adcontextprotocol/adcp-client-python/commit/969f373873cd187bc08e542122c71bd76a4072a2))
* allow owned signals discovery without activation ([#792](https://github.com/adcontextprotocol/adcp-client-python/issues/792)) ([46ae0c9](https://github.com/adcontextprotocol/adcp-client-python/commit/46ae0c9cc3a817c9630f232e6eb5fcc8dbe175a2))
* **decisioning:** timing-oracle defense on PERMISSION_DENIED branches ([#772](https://github.com/adcontextprotocol/adcp-client-python/issues/772)) ([#774](https://github.com/adcontextprotocol/adcp-client-python/issues/774)) ([c23c9d5](https://github.com/adcontextprotocol/adcp-client-python/commit/c23c9d524ed6ba390cce0ad224d809408160a8ef))
* preserve full sync_accounts request for account stores ([#796](https://github.com/adcontextprotocol/adcp-client-python/issues/796)) ([2b7ad01](https://github.com/adcontextprotocol/adcp-client-python/commit/2b7ad01dc19f0d7146dcb6a6dd5734e3ed7220d5))
* **signing:** Argus follow-ups on [#789](https://github.com/adcontextprotocol/adcp-client-python/issues/789) — IDNA + IP literals, Protocol contract, misconfig warnings, diagnostic hygiene ([#798](https://github.com/adcontextprotocol/adcp-client-python/issues/798)) ([fcb64fc](https://github.com/adcontextprotocol/adcp-client-python/commit/fcb64fcfae8b42822bdf92c289dcb771191419c4))


### Reverts

* **preview:** remove opt-in v3.1 preview surface ([#747](https://github.com/adcontextprotocol/adcp-client-python/issues/747)) ([#755](https://github.com/adcontextprotocol/adcp-client-python/issues/755)) ([6947870](https://github.com/adcontextprotocol/adcp-client-python/commit/6947870bb2d3a5746ab174b3dee747a50e9c63fc))


### Documentation

* **agents:** behaviour → behavior (Argus smoke test) ([#766](https://github.com/adcontextprotocol/adcp-client-python/issues/766)) ([e7c0815](https://github.com/adcontextprotocol/adcp-client-python/commit/e7c0815595f9839f065323f9e90f087b71773d5b))

## [5.6.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.5.0...v5.6.0) (2026-05-19)


### Features

* **auth:** per-instance discovery_tools override for BearerTokenAuth ([#743](https://github.com/adcontextprotocol/adcp-client-python/issues/743)) ([76d0673](https://github.com/adcontextprotocol/adcp-client-python/commit/76d067344390693f811d412b7d4deea64e0e2633))


### Bug Fixes

* **types:** open Format.assets discriminated union with UnknownFormatAsset fallback arm (issue [#742](https://github.com/adcontextprotocol/adcp-client-python/issues/742)) ([#744](https://github.com/adcontextprotocol/adcp-client-python/issues/744)) ([7904f85](https://github.com/adcontextprotocol/adcp-client-python/commit/7904f85ca0a9241977fb70e782a687425a4a7565))
* **types:** resolve capability sub-models via field annotation, not numbered names ([#745](https://github.com/adcontextprotocol/adcp-client-python/issues/745)) ([b63c7e6](https://github.com/adcontextprotocol/adcp-client-python/commit/b63c7e6bf4df453ad40760e70fd638080deecdfa))


### Documentation

* **decisioning:** document AccountStore.resolve() as the multi-tenant encoding seam ([#739](https://github.com/adcontextprotocol/adcp-client-python/issues/739)) ([0b30743](https://github.com/adcontextprotocol/adcp-client-python/commit/0b30743818c0b3c85ce2ddb05f4305badbe6504f))

## [5.5.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.4.0...v5.5.0) (2026-05-14)


### Features

* **agents:** adopt shared .agents/roles/ layout from adcp[#4500](https://github.com/adcontextprotocol/adcp-client-python/issues/4500) ([#730](https://github.com/adcontextprotocol/adcp-client-python/issues/730)) ([fe02c17](https://github.com/adcontextprotocol/adcp-client-python/commit/fe02c173bfc5a33bfdd378f12f01bea5f2663876))
* **agents:** Phase 3 — weekly sync workflow from adcp ([#731](https://github.com/adcontextprotocol/adcp-client-python/issues/731)) ([884873a](https://github.com/adcontextprotocol/adcp-client-python/commit/884873a12d51adafef51c298220f21dae6096d4b))
* **decisioning:** PgProposalStore + framework package derivation ([#727](https://github.com/adcontextprotocol/adcp-client-python/issues/727)) ([#732](https://github.com/adcontextprotocol/adcp-client-python/issues/732)) ([c41d4a8](https://github.com/adcontextprotocol/adcp-client-python/commit/c41d4a8e01148e71bbf7e97df95e4f241fe11bf1))


### Bug Fixes

* **v3-ref-seller:** run alembic upgrade head at boot ([#421](https://github.com/adcontextprotocol/adcp-client-python/issues/421)) ([#733](https://github.com/adcontextprotocol/adcp-client-python/issues/733)) ([ffa3c8c](https://github.com/adcontextprotocol/adcp-client-python/commit/ffa3c8c49ebaba3735721c92176086807782cdbf))


### Documentation

* response-extension import path guidance for context-specific schema variants ([#644](https://github.com/adcontextprotocol/adcp-client-python/issues/644)) ([61fb835](https://github.com/adcontextprotocol/adcp-client-python/commit/61fb835fb7c141ddec95b6e890eb804024b21edb))

## [5.4.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.3.0...v5.4.0) (2026-05-13)


### Features

* **decisioning:** LazyPlatformRouter.proposal_stores= + proposal_store_factory= ([#722](https://github.com/adcontextprotocol/adcp-client-python/issues/722)) ([#724](https://github.com/adcontextprotocol/adcp-client-python/issues/724)) ([a65185e](https://github.com/adcontextprotocol/adcp-client-python/commit/a65185e83637bfe039c9d349b468a8e90cecb1ee))
* **decisioning:** ProposalCapabilities.auto_commit_on_put_draft ([#723](https://github.com/adcontextprotocol/adcp-client-python/issues/723)) ([#725](https://github.com/adcontextprotocol/adcp-client-python/issues/725)) ([5567e2b](https://github.com/adcontextprotocol/adcp-client-python/commit/5567e2b91e979697091641039092a8ee5c5586cf))
* **idempotency:** inject 'replayed: true' on cache hit ([#714](https://github.com/adcontextprotocol/adcp-client-python/issues/714)) ([#717](https://github.com/adcontextprotocol/adcp-client-python/issues/717)) ([b511e01](https://github.com/adcontextprotocol/adcp-client-python/commit/b511e010fff3c336a6308e01139642c1c689bbd8))
* **server:** on_startup / on_shutdown hooks on serve(transport='both') ([#713](https://github.com/adcontextprotocol/adcp-client-python/issues/713)) ([aca625e](https://github.com/adcontextprotocol/adcp-client-python/commit/aca625edf18b7f62f44c829a2317fc770a72ee05))
* **types:** SchemaVariant marker + mypy plugin for cross-class overrides ([#710](https://github.com/adcontextprotocol/adcp-client-python/issues/710)) ([#718](https://github.com/adcontextprotocol/adcp-client-python/issues/718)) ([0b53cc9](https://github.com/adcontextprotocol/adcp-client-python/commit/0b53cc9187cbe40da28db666d31030ebd6ff0d83))


### Bug Fixes

* **decisioning:** async-safe create_adcp_server_from_platform ([#700](https://github.com/adcontextprotocol/adcp-client-python/issues/700)) ([#719](https://github.com/adcontextprotocol/adcp-client-python/issues/719)) ([056714f](https://github.com/adcontextprotocol/adcp-client-python/commit/056714f8669aad0c3dd2f0a1b545a3d8c1682826))
* **server:** Authorization: Bearer always accepted; legacy headers additive ([#720](https://github.com/adcontextprotocol/adcp-client-python/issues/720)) ([#721](https://github.com/adcontextprotocol/adcp-client-python/issues/721)) ([dd6503e](https://github.com/adcontextprotocol/adcp-client-python/commit/dd6503ecf36094d2e00ec042b26ac1f8465f344e))
* **server:** emit WWW-Authenticate on every 401 (RFC 6750 §3) [#712](https://github.com/adcontextprotocol/adcp-client-python/issues/712) ([#715](https://github.com/adcontextprotocol/adcp-client-python/issues/715)) ([ce707d3](https://github.com/adcontextprotocol/adcp-client-python/commit/ce707d3b2a08e7b2a73ecbee53b361821e393dba))

## [5.3.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.2.0...v5.3.0) (2026-05-12)


### Features

* **adagents:** add validate_adagents_structure helper ([#708](https://github.com/adcontextprotocol/adcp-client-python/issues/708)) ([ccd1955](https://github.com/adcontextprotocol/adcp-client-python/commit/ccd19559b97417f9b6a0141e857d2c23cb78289f))
* **adagents:** ads.txt MANAGERDOMAIN fallback discovery ([#704](https://github.com/adcontextprotocol/adcp-client-python/issues/704)) ([#705](https://github.com/adcontextprotocol/adcp-client-python/issues/705)) ([fddea1a](https://github.com/adcontextprotocol/adcp-client-python/commit/fddea1ab2f44db12fc932767b505009a457f2540))
* **buyer-agent-registry:** with_caching factory auto-invalidates on mutations ([#692](https://github.com/adcontextprotocol/adcp-client-python/issues/692)) ([de65957](https://github.com/adcontextprotocol/adcp-client-python/commit/de65957e26209befd9a1e3cca34df9ca5361de50))
* **server:** boot validator for webhook_signing.supported capability invariant ([#695](https://github.com/adcontextprotocol/adcp-client-python/issues/695)) ([7e9a734](https://github.com/adcontextprotocol/adcp-client-python/commit/7e9a73491feeacc9b1b595178c01733c88a2da1f))
* **testing:** add SellerA2AClient for in-process A2A handler testing ([#694](https://github.com/adcontextprotocol/adcp-client-python/issues/694)) ([2d1ae2f](https://github.com/adcontextprotocol/adcp-client-python/commit/2d1ae2faa7cb6526b56a9311db3667eff9cb6f89))
* **testing:** add SellerTestClient for in-process handler testing ([#666](https://github.com/adcontextprotocol/adcp-client-python/issues/666)) ([965eeda](https://github.com/adcontextprotocol/adcp-client-python/commit/965eeda0b8253580da923c5b322618ea6d6d1aa6))
* **types:** Sequence[T] on response-only list fields for covariant adoption ([#635](https://github.com/adcontextprotocol/adcp-client-python/issues/635)) ([19be8db](https://github.com/adcontextprotocol/adcp-client-python/commit/19be8dbff689066b85e7145447f1ef582b710bfb))


### Bug Fixes

* **ci:** v3 reference seller storyboard job actually asserts on results ([#693](https://github.com/adcontextprotocol/adcp-client-python/issues/693)) ([27a5866](https://github.com/adcontextprotocol/adcp-client-python/commit/27a58661d661dfe2dbea7f84b9108a98dacd8ffe))
* **compat:** extract hostname from brand_manifest URLs with paths ([#679](https://github.com/adcontextprotocol/adcp-client-python/issues/679)) ([6bb2c26](https://github.com/adcontextprotocol/adcp-client-python/commit/6bb2c2624fd044bec443281b88f636388754b9ec)), closes [#677](https://github.com/adcontextprotocol/adcp-client-python/issues/677)
* **compat:** handle inline BrandManifest object in v2.5 adapters ([#685](https://github.com/adcontextprotocol/adcp-client-python/issues/685)) ([edd7d0a](https://github.com/adcontextprotocol/adcp-client-python/commit/edd7d0a62ebb5ca06b285ba9a0db3d264de0e53a))
* **compat:** warn on non-standard brand_manifest path in inline-object branch ([#688](https://github.com/adcontextprotocol/adcp-client-python/issues/688)) ([840e6b3](https://github.com/adcontextprotocol/adcp-client-python/commit/840e6b30ec42c09e9a9de3592b0756fb1f776053)), closes [#687](https://github.com/adcontextprotocol/adcp-client-python/issues/687)
* **compat:** warn when brand_manifest non-standard path is flattened to domain ([#686](https://github.com/adcontextprotocol/adcp-client-python/issues/686)) ([0568d2e](https://github.com/adcontextprotocol/adcp-client-python/commit/0568d2eafea58a66b96ebd38afef218558f06edf))
* **server:** preserve Starlette lifespan when public_url is callable ([#680](https://github.com/adcontextprotocol/adcp-client-python/issues/680)) ([8632847](https://github.com/adcontextprotocol/adcp-client-python/commit/86328470ff9b53f6837368edd14851c16032efa7)), closes [#676](https://github.com/adcontextprotocol/adcp-client-python/issues/676)

## [5.2.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.1.0...v5.2.0) (2026-05-11)


### Features

* **client:** server_version constructor scaffold (stage 7-lite) ([#674](https://github.com/adcontextprotocol/adcp-client-python/issues/674)) ([5c7acb4](https://github.com/adcontextprotocol/adcp-client-python/commit/5c7acb4be3e86d55f039e0643758605383c8c098))
* **client:** surface structured adcp_error on TaskResult ([#675](https://github.com/adcontextprotocol/adcp-client-python/issues/675)) ([fe66335](https://github.com/adcontextprotocol/adcp-client-python/commit/fe66335006d188fdc77194403e77f08b44bfce6b))
* **compat:** AdapterPair pattern + v2.5 sync_creatives (stage 4) ([#665](https://github.com/adcontextprotocol/adcp-client-python/issues/665)) ([0e47dcc](https://github.com/adcontextprotocol/adcp-client-python/commit/0e47dcc1cf19c2d6b31b90749af1b6733891694e))
* **compat:** server-side shape-based v2.5 detection (stage 6) ([#673](https://github.com/adcontextprotocol/adcp-client-python/issues/673)) ([d6210cc](https://github.com/adcontextprotocol/adcp-client-python/commit/d6210cce69ae04a64536a5f35edc45d1bd0d67cc))
* **compat:** v2.5 create_media_buy + update_media_buy adapters (stage 5c) ([#669](https://github.com/adcontextprotocol/adcp-client-python/issues/669)) ([4e3473a](https://github.com/adcontextprotocol/adcp-client-python/commit/4e3473a3a35b478bb6a6ef174af6790725d62a27))
* **compat:** v2.5 get_products adapter (stage 5b) ([#668](https://github.com/adcontextprotocol/adcp-client-python/issues/668)) ([2025520](https://github.com/adcontextprotocol/adcp-client-python/commit/2025520985063303c162b46d68546734344f309a))
* **compat:** v2.5 list_creative_formats + preview_creative adapters; deprecate spec_compat_hooks ([#667](https://github.com/adcontextprotocol/adcp-client-python/issues/667)) ([fec91d6](https://github.com/adcontextprotocol/adcp-client-python/commit/fec91d67cd0192b74470aa52c04688dbc34b37f4))
* **schemas:** fetch + bundle v2.5 schemas from pinned upstream SHA (stage 4b1) ([#670](https://github.com/adcontextprotocol/adcp-client-python/issues/670)) ([bd6837f](https://github.com/adcontextprotocol/adcp-client-python/commit/bd6837fcab043b3a4cf3b6000daf2e5a92d96b6f))
* **server:** pre-adapter validation against legacy schema (stage 4b2) ([#671](https://github.com/adcontextprotocol/adcp-client-python/issues/671)) ([4ef032b](https://github.com/adcontextprotocol/adcp-client-python/commit/4ef032b2c153296616ab51ab0fe06489be5587ab))
* **server:** route validation by wire adcp_version (stage 3) ([#664](https://github.com/adcontextprotocol/adcp-client-python/issues/664)) ([d2ffac7](https://github.com/adcontextprotocol/adcp-client-python/commit/d2ffac70d9a71d1360ad2e1056acdcdf5381df45))
* **validation:** per-version validator loader (stage 2 of versioned validation) ([#659](https://github.com/adcontextprotocol/adcp-client-python/issues/659)) ([6311a9a](https://github.com/adcontextprotocol/adcp-client-python/commit/6311a9a8ea4b14af9c2abc6991231e21585cbea7))

## [5.1.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v5.0.0...v5.1.0) (2026-05-11)


### Features

* **a2a:** per-request agent-card URL resolution via callable public_url ([#650](https://github.com/adcontextprotocol/adcp-client-python/issues/650)) ([1b4f3e0](https://github.com/adcontextprotocol/adcp-client-python/commit/1b4f3e05bd075b65b334a289fa05403008918796))
* **server:** add spec_compat_hooks() for pre-v3 / pre-4.4 buyer compatibility ([#648](https://github.com/adcontextprotocol/adcp-client-python/issues/648)) ([30690e5](https://github.com/adcontextprotocol/adcp-client-python/commit/30690e54a0298960489e8595c3f5480a401ad00f))
* **server:** TenantRegistry.as_platform() adapter for serve() integration ([#649](https://github.com/adcontextprotocol/adcp-client-python/issues/649)) ([0e396ca](https://github.com/adcontextprotocol/adcp-client-python/commit/0e396ca778ea0a4def1f44d315b0d69b6cc00443))
* **testing:** forward pre_validation_hooks through build_asgi_app ([#655](https://github.com/adcontextprotocol/adcp-client-python/issues/655)) ([2df49c0](https://github.com/adcontextprotocol/adcp-client-python/commit/2df49c03d4c34902c0869a6f25e65488ad303968))


### Bug Fixes

* **decisioning:** wrap pydantic.ValidationError from delegates as INVALID_REQUEST ([#656](https://github.com/adcontextprotocol/adcp-client-python/issues/656)) ([976ab4f](https://github.com/adcontextprotocol/adcp-client-python/commit/976ab4f611c93fcfe461cb5e5028b1a13bb90f0a))

## [5.0.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.6.1...v5.0.0) (2026-05-10)


### ⚠ BREAKING CHANGES

* **types:** callers passing `affected_packages` into a function typed `def f(x: list[Package])` will see a mypy error and need to migrate to `Sequence[Package]`. Runtime behavior is unchanged; the change is annotation-only.
* **types:** Subclasses that add fields without Field(exclude=True) will now have those fields appear in model_dump() output where they were previously dropped by Pydantic's declared-schema firewall. Audit each subclass and mark internal fields with Field(exclude=True). To restore the prior behavior at a specific call site, pass serialize_as_any=False explicitly.
* **webhooks:** `domain` kwarg removed from `create_mcp_webhook_payload` and `WebhookSender.send_mcp`. Migrate to `protocol` (kebab-case string or `AdcpProtocol` enum value).
* **webhooks:** `create_mcp_webhook_payload` returns a Pydantic model, not a dict; `task_type` is now required.

### Features

* **a2a:** add public_url param to agent card for production deployments ([#621](https://github.com/adcontextprotocol/adcp-client-python/issues/621)) ([14d294c](https://github.com/adcontextprotocol/adcp-client-python/commit/14d294c7922ad0fa91b30ad1f03de75ff9c7b694))
* **server:** add TenantRegistry with per-tenant health tracking ([#628](https://github.com/adcontextprotocol/adcp-client-python/issues/628)) ([ae687b6](https://github.com/adcontextprotocol/adcp-client-python/commit/ae687b6be5b0807717fd61e3a8dbb0e34ef5ac93))
* **server:** default MCP streamable-http to stateful with idle eviction ([#636](https://github.com/adcontextprotocol/adcp-client-python/issues/636)) ([3173a54](https://github.com/adcontextprotocol/adcp-client-python/commit/3173a545ade4c97a7da593279604359f48e40eff))
* **server:** expose RequestContext.transport and current_transport ContextVar ([#627](https://github.com/adcontextprotocol/adcp-client-python/issues/627)) ([20e5d53](https://github.com/adcontextprotocol/adcp-client-python/commit/20e5d53fa6eff6d7ea047c62d5a2a02e7f9a2c8e))
* **server:** pre-validation request hook for spec-default injection ([#614](https://github.com/adcontextprotocol/adcp-client-python/issues/614)) ([#629](https://github.com/adcontextprotocol/adcp-client-python/issues/629)) ([05d4cd8](https://github.com/adcontextprotocol/adcp-client-python/commit/05d4cd8617440aaa638086ffa5cabc0bf61b403f))
* **testing:** adopter type-checking test suite with zero-ignore contract ([#634](https://github.com/adcontextprotocol/adcp-client-python/issues/634)) ([20e496c](https://github.com/adcontextprotocol/adcp-client-python/commit/20e496c1cf49ae3febf5d3f85930d4c962dd5238))
* **testing:** extend build_asgi_app with full serve-layer kwargs ([#626](https://github.com/adcontextprotocol/adcp-client-python/issues/626)) ([8679a95](https://github.com/adcontextprotocol/adcp-client-python/commit/8679a9565b43d9bc3cda3bde032ac666684c5845))
* **types:** default serialize_as_any=True in AdCPBaseModel.model_dump ([#639](https://github.com/adcontextprotocol/adcp-client-python/issues/639)) ([3160ace](https://github.com/adcontextprotocol/adcp-client-python/commit/3160ace6fed398b738d24d3d101e82a54caedc93)), closes [#615](https://github.com/adcontextprotocol/adcp-client-python/issues/615)
* **types:** widen extension-point list[X] to Sequence[X] ([#624](https://github.com/adcontextprotocol/adcp-client-python/issues/624)) ([#640](https://github.com/adcontextprotocol/adcp-client-python/issues/640)) ([96ccfd4](https://github.com/adcontextprotocol/adcp-client-python/commit/96ccfd48da0a1d1f86a23119b2313a0fe5338416))
* **webhooks:** create_mcp_webhook_payload returns McpWebhookPayload ([#632](https://github.com/adcontextprotocol/adcp-client-python/issues/632)) ([9eb962c](https://github.com/adcontextprotocol/adcp-client-python/commit/9eb962c38eb06e9e76290dcd0830edefd0c6a778))
* **webhooks:** replace `domain` kwarg with typed `protocol` (AdcpProtocol enum) ([#637](https://github.com/adcontextprotocol/adcp-client-python/issues/637)) ([fdd4053](https://github.com/adcontextprotocol/adcp-client-python/commit/fdd405386e8a6d08c55d6b3f43c1b619b6541c86))


### Bug Fixes

* **server:** register /.well-known/agent.json alias route in create_a2a_server ([#613](https://github.com/adcontextprotocol/adcp-client-python/issues/613)) ([2989101](https://github.com/adcontextprotocol/adcp-client-python/commit/29891019daa7e5c201045f69affd982e41cd459c))
* **server:** strip None-valued asset fields from dict-based response builder output ([#631](https://github.com/adcontextprotocol/adcp-client-python/issues/631)) ([c02ea84](https://github.com/adcontextprotocol/adcp-client-python/commit/c02ea842ada85570b1c953d2f0ec989cba61b8de)), closes [#622](https://github.com/adcontextprotocol/adcp-client-python/issues/622)
* **types:** widen canceled Literal[True]=True to Literal[True]|None=None on request types ([#643](https://github.com/adcontextprotocol/adcp-client-python/issues/643)) ([120ae3b](https://github.com/adcontextprotocol/adcp-client-python/commit/120ae3b690f3ec5ae70d0f825ff4e19506205ead))


### Documentation

* **types:** document Field(exclude=True) and [@model](https://github.com/model)_serializer for nested wire isolation ([#630](https://github.com/adcontextprotocol/adcp-client-python/issues/630)) ([4912af9](https://github.com/adcontextprotocol/adcp-client-python/commit/4912af9f3f3fb65588281e5bb96885cf3d955fd9))

## [4.6.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.6.0...v4.6.1) (2026-05-10)


### Bug Fixes

* **decisioning:** wire sync_accounts/list_accounts dispatch to AccountStore Protocols ([#610](https://github.com/adcontextprotocol/adcp-client-python/issues/610)) ([dabf4fb](https://github.com/adcontextprotocol/adcp-client-python/commit/dabf4fb77cb8d64ff1984aa52ae74aac6230b8a1))

## [4.6.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.5.0...v4.6.0) (2026-05-09)


### Features

* **webhooks:** public to_wire_dict() serialization seam ([#602](https://github.com/adcontextprotocol/adcp-client-python/issues/602)) ([6a06e88](https://github.com/adcontextprotocol/adcp-client-python/commit/6a06e888a6114c083d6bcf76c53b56d8433e3f99))


### Bug Fixes

* **decisioning:** re-validate params through platform method's stricter subclass annotation ([#597](https://github.com/adcontextprotocol/adcp-client-python/issues/597)) ([3d269f5](https://github.com/adcontextprotocol/adcp-client-python/commit/3d269f5c2ddd1624e6f6caf0a370214b4ab1751d))
* **webhooks:** add canceled/rejected/auth-required to A2A status map; fail fast on unknowns ([#606](https://github.com/adcontextprotocol/adcp-client-python/issues/606)) ([89f9491](https://github.com/adcontextprotocol/adcp-client-python/commit/89f949105fa99c9fd2da4e0d3bef207be4471242))
* **webhooks:** correct type annotations for extract_webhook_result_data and payload builders ([#600](https://github.com/adcontextprotocol/adcp-client-python/issues/600)) ([e624b5c](https://github.com/adcontextprotocol/adcp-client-python/commit/e624b5c4e72754f0f5b3fd6b6bebc8abddefef5e))
* **webhooks:** reject unknown AdCP status in create_a2a_webhook_payload ([#605](https://github.com/adcontextprotocol/adcp-client-python/issues/605)) ([37d2cda](https://github.com/adcontextprotocol/adcp-client-python/commit/37d2cdacc609f7506d1c3556f2b5aa4b7fd95153))


### Documentation

* **contributing:** document PR title format to prevent silent CHANGELOG drops ([#580](https://github.com/adcontextprotocol/adcp-client-python/issues/580)) ([2c5ec64](https://github.com/adcontextprotocol/adcp-client-python/commit/2c5ec64a8e3a7109e0834bbbe7aa761ac87eebd1))

## [4.5.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.4.3...v4.5.0) (2026-05-07)


### Features

* **auth:** per-leg header config + agent-card bearerAuth scheme ([#595](https://github.com/adcontextprotocol/adcp-client-python/issues/595)) ([52f45ef](https://github.com/adcontextprotocol/adcp-client-python/commit/52f45efbc4e2ba20bbb824e3b86b17d171d7570b))


### Bug Fixes

* **client:** preserve agent_uri trailing slash; widen MCP URL fallbacks ([#582](https://github.com/adcontextprotocol/adcp-client-python/issues/582)) ([1db3ce3](https://github.com/adcontextprotocol/adcp-client-python/commit/1db3ce3d585bdc8af2425078981b53e69f1f49ce))

## [4.4.3](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.4.2...v4.4.3) (2026-05-05)


### Bug Fixes

* **server:** A2A auth middleware populates current_principal contextvars ([#592](https://github.com/adcontextprotocol/adcp-client-python/issues/592)) ([5430942](https://github.com/adcontextprotocol/adcp-client-python/commit/543094270f97685034e7e390199e1909222a451b))

## [4.4.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.4.1...v4.4.2) (2026-05-05)


### Bug Fixes

* **deps:** re-pin a2a-sdk&lt;1.0.2, add protobuf&gt;=6 floor + matrix canary ([#588](https://github.com/adcontextprotocol/adcp-client-python/issues/588)) ([1e16d6f](https://github.com/adcontextprotocol/adcp-client-python/commit/1e16d6f3523aee060d1912c9608127caed4190ea))

## [4.4.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.4.0...v4.4.1) (2026-05-05)


### Bug Fixes

* **client:** add extra_headers escape hatch for multi-tenant servers ([#585](https://github.com/adcontextprotocol/adcp-client-python/issues/585)) ([66b7456](https://github.com/adcontextprotocol/adcp-client-python/commit/66b745692cb5e3d1099424efdaed8e4ab0faa3a3))
* **deps:** bump a2a-sdk to &gt;=1.0.2,&lt;1.1 ([#586](https://github.com/adcontextprotocol/adcp-client-python/issues/586)) ([2fd640c](https://github.com/adcontextprotocol/adcp-client-python/commit/2fd640ce762eaaa0370869a01355da1f4afb8e35))

## [4.4.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.3.0...v4.4.0) (2026-05-04)


### Features

* **auth:** header_name + bearer_prefix_required on BearerTokenAuthMiddleware ([#545](https://github.com/adcontextprotocol/adcp-client-python/issues/545)) ([b16d18f](https://github.com/adcontextprotocol/adcp-client-python/commit/b16d18f88f4c905843e297c06341e475a89a991b))
* **buyer-agent-registry:** caching + rate-limit + audit emission ([#380](https://github.com/adcontextprotocol/adcp-client-python/issues/380)) ([#407](https://github.com/adcontextprotocol/adcp-client-python/issues/407)) ([5a1e6b6](https://github.com/adcontextprotocol/adcp-client-python/commit/5a1e6b620c86da88259f7477f91179762c4a04e2))
* **client:** looks_like_v3_capabilities — drop the v2-downgrade footgun on capabilities-validation failure ([#475](https://github.com/adcontextprotocol/adcp-client-python/issues/475)) ([385dc80](https://github.com/adcontextprotocol/adcp-client-python/commit/385dc80f9c2043d175b7420dda7cfa3a8d387403))
* **decisioning:** Account v3 projection helpers — bank-details write-only guard ([#356](https://github.com/adcontextprotocol/adcp-client-python/issues/356)) ([#366](https://github.com/adcontextprotocol/adcp-client-python/issues/366)) ([5d898b6](https://github.com/adcontextprotocol/adcp-client-python/commit/5d898b6dda23db54e56d155a1de151a14721cf56))
* **decisioning:** Account.mode + sandbox-authority gate for comply_test_controller (Phase 1) ([#483](https://github.com/adcontextprotocol/adcp-client-python/issues/483)) ([f5cd8cf](https://github.com/adcontextprotocol/adcp-client-python/commit/f5cd8cf7f4f2e03425302b5945d109c5f90cf2f9))
* **decisioning:** adopt structured capabilities in v3 reference seller ([8316105](https://github.com/adcontextprotocol/adcp-client-python/commit/83161053ecda45f10b2d263e902c628a6b158ef5)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **decisioning:** advertise_all kwarg + handler.get_advertised_tools ([#529](https://github.com/adcontextprotocol/adcp-client-python/issues/529)) ([8b1b384](https://github.com/adcontextprotocol/adcp-client-python/commit/8b1b384f410818ae0a35ad4bcaf1a496d706f3c6))
* **decisioning:** align AccountStore.resolution literal with JS + Tier 1 docs ([#330](https://github.com/adcontextprotocol/adcp-client-python/issues/330)) ([6490948](https://github.com/adcontextprotocol/adcp-client-python/commit/6490948ae5d9abb668775db50652c36b989a80e0))
* **decisioning:** AuthInfo.from_verified_signer — bridge to RFC 9421 verifier ([#365](https://github.com/adcontextprotocol/adcp-client-python/issues/365)) ([5ae109b](https://github.com/adcontextprotocol/adcp-client-python/commit/5ae109b893cedc998ffb0fa50699cb8af7cc8839))
* **decisioning:** boot-time capabilities response shape validation ([#422](https://github.com/adcontextprotocol/adcp-client-python/issues/422)) ([#446](https://github.com/adcontextprotocol/adcp-client-python/issues/446)) ([127f164](https://github.com/adcontextprotocol/adcp-client-python/commit/127f164faa18b51e24e050e934d9dbd41956c9e2))
* **decisioning:** boot-time validator for declared idempotency vs wired [@wrap](https://github.com/wrap) ([#543](https://github.com/adcontextprotocol/adcp-client-python/issues/543)) ([7657ad4](https://github.com/adcontextprotocol/adcp-client-python/commit/7657ad446b9bfd449b0170be7659838e093dc363))
* **decisioning:** BrandRights/ContentStandards/PropertyLists/CollectionLists Protocols (breadth sprint Batch 4 — FINAL) ([#335](https://github.com/adcontextprotocol/adcp-client-python/issues/335)) ([aeb2543](https://github.com/adcontextprotocol/adcp-client-python/commit/aeb25434847828928bca45644769e77e22a09905))
* **decisioning:** built-in fields projection on get_products responses ([#503](https://github.com/adcontextprotocol/adcp-client-python/issues/503)) ([06aaf51](https://github.com/adcontextprotocol/adcp-client-python/commit/06aaf5193890f96ab5730574498346e7380aeffb))
* **decisioning:** CampaignGovernancePlatform Protocol (breadth sprint Batch 3) ([#334](https://github.com/adcontextprotocol/adcp-client-python/issues/334)) ([2ca828a](https://github.com/adcontextprotocol/adcp-client-python/commit/2ca828a6ab84f49521b819aecd917924c02d80b0))
* **decisioning:** composeMethod + security composer helpers ([#466](https://github.com/adcontextprotocol/adcp-client-python/issues/466)) ([ddad4b9](https://github.com/adcontextprotocol/adcp-client-python/commit/ddad4b9853fb0df022eae5be3dec5ee660b76dc7))
* **decisioning:** comprehensive Emma DX follow-up (P0/P1/P2 + examples) ([#339](https://github.com/adcontextprotocol/adcp-client-python/issues/339)) ([985dcd9](https://github.com/adcontextprotocol/adcp-client-python/commit/985dcd9a287a89d1fb79e321f856075c3b31af71))
* **decisioning:** create_tenant_store — opinionated multi-tenant AccountStore with fail-closed isolation ([#473](https://github.com/adcontextprotocol/adcp-client-python/issues/473)) ([8dd5dab](https://github.com/adcontextprotocol/adcp-client-python/commit/8dd5dabc06f38f7045ec000190f6c8ad6565baa7))
* **decisioning:** createOAuthPassthroughResolver — Shape B account resolver factory ([#472](https://github.com/adcontextprotocol/adcp-client-python/issues/472)) ([defa022](https://github.com/adcontextprotocol/adcp-client-python/commit/defa022b5325d89a23c595521bd969eb2482d0dd))
* **decisioning:** createUpstreamHttpClient + createTranslationMap ([#464](https://github.com/adcontextprotocol/adcp-client-python/issues/464)) ([7e5f2c4](https://github.com/adcontextprotocol/adcp-client-python/commit/7e5f2c4fad15ce5ce8d14b19955fafb66513e329))
* **decisioning:** CreativeBuilderPlatform + CreativeAdServerPlatform (breadth sprint Batch 2) ([#333](https://github.com/adcontextprotocol/adcp-client-python/issues/333)) ([5921949](https://github.com/adcontextprotocol/adcp-client-python/commit/592194904ee70424d50f8421cf57812c0668a010))
* **decisioning:** credential-leak strip on every echo path + ctx_metadata guidance ([#481](https://github.com/adcontextprotocol/adcp-client-python/issues/481)) ([52b15fa](https://github.com/adcontextprotocol/adcp-client-python/commit/52b15fa2f252b4e213f2dd2a5795dba08700019e))
* **decisioning:** cursor-based pagination helper for list responses ([#499](https://github.com/adcontextprotocol/adcp-client-python/issues/499)) ([524c608](https://github.com/adcontextprotocol/adcp-client-python/commit/524c608fb1ea2fdf9c8c7659172bec731f4a2ff3))
* **decisioning:** F12 — auto-emit completion webhook on sync mutating responses ([#331](https://github.com/adcontextprotocol/adcp-client-python/issues/331)) ([9d29754](https://github.com/adcontextprotocol/adcp-client-python/commit/9d29754e310e370ca8b9730d61070513640d1e45))
* **decisioning:** handoff_to_workflow — externally-completed task primitive ([#336](https://github.com/adcontextprotocol/adcp-client-python/issues/336)) ([bff48ab](https://github.com/adcontextprotocol/adcp-client-python/commit/bff48abbc26a33a7649ca0264329bcba294dc6fa))
* **decisioning:** LazyPlatformRouter for tenant-on-first-request construction ([#552](https://github.com/adcontextprotocol/adcp-client-python/issues/552)) ([98fd456](https://github.com/adcontextprotocol/adcp-client-python/commit/98fd4566c57925da84a34cabca37281901882350))
* **decisioning:** mock-mode upstream URL routing — Account.metadata['mock_upstream_url'] + DecisioningPlatform.upstream_for (Phase 2) ([#487](https://github.com/adcontextprotocol/adcp-client-python/issues/487)) ([5c605b4](https://github.com/adcontextprotocol/adcp-client-python/commit/5c605b463c2fa48bc079ed301cffc5608a5b001c))
* **decisioning:** MockAdServer Protocol + /_debug/traffic counters ([#383](https://github.com/adcontextprotocol/adcp-client-python/issues/383)) ([#405](https://github.com/adcontextprotocol/adcp-client-python/issues/405)) ([53d8b8c](https://github.com/adcontextprotocol/adcp-client-python/commit/53d8b8cbe7be5208e3d1250a7e1068f93c33ca28))
* **decisioning:** PgBuyerAgentRegistry — durable Tier 2 commercial-identity store ([#364](https://github.com/adcontextprotocol/adcp-client-python/issues/364)) ([fbdcb31](https://github.com/adcontextprotocol/adcp-client-python/commit/fbdcb31f2e4d18a8cff6875aa6ad5fd7b5643e5f))
* **decisioning:** PlatformRouter + multi-platform proof ([#477](https://github.com/adcontextprotocol/adcp-client-python/issues/477)) ([#490](https://github.com/adcontextprotocol/adcp-client-python/issues/490)) ([786e6a2](https://github.com/adcontextprotocol/adcp-client-python/commit/786e6a2708522e4b6308b91534b1f412ca58ae9d))
* **decisioning:** PostgresTaskRegistry — durable HITL task state (v6.1) ([#361](https://github.com/adcontextprotocol/adcp-client-python/issues/361)) ([e01eef0](https://github.com/adcontextprotocol/adcp-client-python/commit/e01eef0a3f4e3d4eb3acfb66f22d9486f101d54f))
* **decisioning:** ProductConfigStore lookup helper for create_media_buy ([#498](https://github.com/adcontextprotocol/adcp-client-python/issues/498)) ([59f3b6e](https://github.com/adcontextprotocol/adcp-client-python/commit/59f3b6e2968fb5b2ece8f745e6af386a8bf4cf2a))
* **decisioning:** project full capability blocks via DecisioningCapabilities ([c09433c](https://github.com/adcontextprotocol/adcp-client-python/commit/c09433c2526d3a7635ea6cb7a975f840e03dc82d)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **decisioning:** project full get_adcp_capabilities response schema into DecisioningCapabilities ([55613f8](https://github.com/adcontextprotocol/adcp-client-python/commit/55613f8def13d0c388f131709262c9003b26add1))
* **decisioning:** property_list resolver + intersection helper for get_products ([#500](https://github.com/adcontextprotocol/adcp-client-python/issues/500)) ([1dfdcd5](https://github.com/adcontextprotocol/adcp-client-python/commit/1dfdcd5e00242e3e79faba6f4a255fe822194cb0))
* **decisioning:** ProposalManager v1 — Protocol + MockProposalManager forwarder + tenant binding ([#504](https://github.com/adcontextprotocol/adcp-client-python/issues/504)) ([fe8e5b0](https://github.com/adcontextprotocol/adcp-client-python/commit/fe8e5b0382324365a5b86bd4467853b6284f354a))
* **decisioning:** ProposalManager v1.5 — primitives, capability validation, lifecycle helpers ([#538](https://github.com/adcontextprotocol/adcp-client-python/issues/538)-impl) ([#550](https://github.com/adcontextprotocol/adcp-client-python/issues/550)) ([c7c317e](https://github.com/adcontextprotocol/adcp-client-python/commit/c7c317eb1f466c32d696ff135de1d55d8f074889))
* **decisioning:** re-export SignalsFeatures + ContentStandards capability sub-models ([74671d0](https://github.com/adcontextprotocol/adcp-client-python/commit/74671d0efe20fb3f1abcd28650eb8fc121163292)), closes [#485](https://github.com/adcontextprotocol/adcp-client-python/issues/485)
* **decisioning:** refine[] flow scaffold for get_products ([#496](https://github.com/adcontextprotocol/adcp-client-python/issues/496)) ([#505](https://github.com/adcontextprotocol/adcp-client-python/issues/505)) ([b584e70](https://github.com/adcontextprotocol/adcp-client-python/commit/b584e70e8563044434898d0b78c3ca00d3c13eea))
* **decisioning:** retype DecisioningCapabilities.specialisms to Specialism enum union ([9fb7fc9](https://github.com/adcontextprotocol/adcp-client-python/commit/9fb7fc93a9cbc6602cffe0ee88d7d8e118390b77)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **decisioning:** SignalsPlatform + AudiencePlatform Protocols (breadth sprint Batch 1) ([#332](https://github.com/adcontextprotocol/adcp-client-python/issues/332)) ([c66ff72](https://github.com/adcontextprotocol/adcp-client-python/commit/c66ff72688bee46d6e34aad08840938b278dc8fe))
* **decisioning:** state-machine helpers + typed exception classes + ref_account_id ([#465](https://github.com/adcontextprotocol/adcp-client-python/issues/465)) ([9301acf](https://github.com/adcontextprotocol/adcp-client-python/commit/9301acf01576b24ae79718a9792bee385e9d7717))
* **decisioning:** surface capability sub-models via adcp.decisioning.capabilities ([bc5bb9a](https://github.com/adcontextprotocol/adcp-client-python/commit/bc5bb9af06a6c189aa3219221d2fac4078cc5706)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **decisioning:** TaskHandoff create_media_buy on_complete + on_failure hooks ([#561](https://github.com/adcontextprotocol/adcp-client-python/issues/561)) ([3072c6e](https://github.com/adcontextprotocol/adcp-client-python/commit/3072c6e0675a707ec8c933c5c20e49efb3927fff))
* **decisioning:** Tier 2 — BuyerAgentRegistry + dispatch wire-up + AuthInfo v3 fields ([#349](https://github.com/adcontextprotocol/adcp-client-python/issues/349)) ([#359](https://github.com/adcontextprotocol/adcp-client-python/issues/359)) ([9dad22c](https://github.com/adcontextprotocol/adcp-client-python/commit/9dad22c94f15052777cddf4652b007a44d2f1136))
* **decisioning:** time_budget deadline wrapper + incomplete projection on get_products ([#501](https://github.com/adcontextprotocol/adcp-client-python/issues/501)) ([bc6ad13](https://github.com/adcontextprotocol/adcp-client-python/commit/bc6ad13ee18454c2091f5d8951574761ca1c0df0))
* **decisioning:** UserWarning when sales-* required methods missing ([#423](https://github.com/adcontextprotocol/adcp-client-python/issues/423)) ([#445](https://github.com/adcontextprotocol/adcp-client-python/issues/445)) ([5349ddc](https://github.com/adcontextprotocol/adcp-client-python/commit/5349ddc91b0299f18b96e7fb75383971c7446136))
* **decisioning:** v6.0 DecisioningPlatform foundation (skeleton + design) ([#316](https://github.com/adcontextprotocol/adcp-client-python/issues/316)) ([3539359](https://github.com/adcontextprotocol/adcp-client-python/commit/353935951a7358ea749519a702b2bb5b54355a1c))
* **decisioning:** warn when compliance_testing capability is declared without test_controller wired ([37f42a7](https://github.com/adcontextprotocol/adcp-client-python/commit/37f42a76402bcb4a8dbcf90a1af6cf7481fbd4e7)), closes [#485](https://github.com/adcontextprotocol/adcp-client-python/issues/485)
* **decisioning:** warn when supported_protocols is auto-derived from specialisms ([a0eb1e2](https://github.com/adcontextprotocol/adcp-client-python/commit/a0eb1e265562f7a5d8e0fc1ee84b76c44e157d85)), closes [#485](https://github.com/adcontextprotocol/adcp-client-python/issues/485)
* **decisioning:** WebhookDeliverySupervisor + SQLAlchemy A2A stores example ([#348](https://github.com/adcontextprotocol/adcp-client-python/issues/348)) ([73a7512](https://github.com/adcontextprotocol/adcp-client-python/commit/73a7512cd3d5c3fcee265455f7e54d7738c77124))
* **decisioning:** widen DecisioningCapabilities with structured wire fields ([5a5219e](https://github.com/adcontextprotocol/adcp-client-python/commit/5a5219e2f6fb965ff489f5ae19feaaf10c953e3c)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **examples:** v3 reference seller — runnable Tier 2 + v3 wiring ([#357](https://github.com/adcontextprotocol/adcp-client-python/issues/357)) ([#373](https://github.com/adcontextprotocol/adcp-client-python/issues/373)) ([cda07f9](https://github.com/adcontextprotocol/adcp-client-python/commit/cda07f997ce9b5cca40f6149b439e7ac0769ddb3))
* **migrate:** --auto-apply mode for mechanically-rewritable codemod findings ([#540](https://github.com/adcontextprotocol/adcp-client-python/issues/540)) ([d19e71e](https://github.com/adcontextprotocol/adcp-client-python/commit/d19e71e0deed2514fd87a6e30f0e8d145a2e1efb))
* **migrate:** flag MediaBuyStatus.pending_activation split in v3-to-v4 codemod ([#523](https://github.com/adcontextprotocol/adcp-client-python/issues/523)) ([d0659c7](https://github.com/adcontextprotocol/adcp-client-python/commit/d0659c759dbc79f8086ee02a377609c172ea57a2))
* **schemas:** bundle adcp-agents.json as adcp.schemas package, eliminate inlined test copy ([#442](https://github.com/adcontextprotocol/adcp-client-python/issues/442)) ([9a8fef1](https://github.com/adcontextprotocol/adcp-client-python/commit/9a8fef13f74371e13b39e92da9828e0e348229d4))
* **server:** /.well-known/adcp-agents.json discovery endpoint ([#381](https://github.com/adcontextprotocol/adcp-client-python/issues/381)) ([#406](https://github.com/adcontextprotocol/adcp-client-python/issues/406)) ([e6dfaad](https://github.com/adcontextprotocol/adcp-client-python/commit/e6dfaad8679d58f25c417c313ff054aff8b87499))
* **server/a2a:** structured error parity with MCP — emit field/details/retry_after, catch decisioning AdcpError (closes [#530](https://github.com/adcontextprotocol/adcp-client-python/issues/530)) ([#536](https://github.com/adcontextprotocol/adcp-client-python/issues/536)) ([423d975](https://github.com/adcontextprotocol/adcp-client-python/commit/423d9758e654664a1eb8094757679d52b27f394d))
* **server/idempotency:** complete PgBackend for multi-worker durable replay ([#555](https://github.com/adcontextprotocol/adcp-client-python/issues/555)) ([1123458](https://github.com/adcontextprotocol/adcp-client-python/commit/11234586f87d36aa2cdf9e1bd192a60a80913788))
* **server:** Account v3 wire fields + AccountStore.upsert/list/syncGovernance receive ResolveContext ([#469](https://github.com/adcontextprotocol/adcp-client-python/issues/469)) ([62b58dc](https://github.com/adcontextprotocol/adcp-client-python/commit/62b58dc3e211e69814a4993ab32726ed514c297e))
* **server:** add asgi_middleware param to serve() ([#441](https://github.com/adcontextprotocol/adcp-client-python/issues/441)) ([69211a3](https://github.com/adcontextprotocol/adcp-client-python/commit/69211a3e17efc8b8fd7512e501c164e83ec9c222))
* **server:** AuditSink Protocol + LoggingAuditSink/SlackAlertSink reference impls ([#362](https://github.com/adcontextprotocol/adcp-client-python/issues/362)) ([66c3c57](https://github.com/adcontextprotocol/adcp-client-python/commit/66c3c57a70d29d778069665e1e0fb3b93966b1ae))
* **server:** CallableSubdomainTenantRouter for DB-backed tenant lookups ([#544](https://github.com/adcontextprotocol/adcp-client-python/issues/544)) ([80876c1](https://github.com/adcontextprotocol/adcp-client-python/commit/80876c165fa492799c804f44def23d12289a36ac))
* **server:** createMediaBuyStore — opt-in targeting_overlay echo for property-lists / collection-lists sellers ([#474](https://github.com/adcontextprotocol/adcp-client-python/issues/474)) ([4ba19fc](https://github.com/adcontextprotocol/adcp-client-python/commit/4ba19fc199ca715adc54d885c00b6a33b90ca94f))
* **server:** createRosterAccountStore — Shape C explicit AccountStore for publisher-curated rosters ([#471](https://github.com/adcontextprotocol/adcp-client-python/issues/471)) ([7f919cc](https://github.com/adcontextprotocol/adcp-client-python/commit/7f919cc0c13c219b2a183adeb14caaa14964901d))
* **server:** default validation=strict in serve() — wire-conformance by default ([#439](https://github.com/adcontextprotocol/adcp-client-python/issues/439)) ([a29b61f](https://github.com/adcontextprotocol/adcp-client-python/commit/a29b61f944732cdee8bff3f11310a67035349edb))
* **server:** introduce ServeConfig dataclass to consolidate serve() kwargs ([#437](https://github.com/adcontextprotocol/adcp-client-python/issues/437)) ([07db849](https://github.com/adcontextprotocol/adcp-client-python/commit/07db849d51e95df4809dd47d404fa95c5469e5b1))
* **server:** MCP error responses populate structuredContent.adcp_error (closes [#509](https://github.com/adcontextprotocol/adcp-client-python/issues/509)) ([#525](https://github.com/adcontextprotocol/adcp-client-python/issues/525)) ([b50a87a](https://github.com/adcontextprotocol/adcp-client-python/commit/b50a87ad63a5f00d53e03398d8ee7d9b055879d2))
* **server:** SubdomainTenantRouter middleware for multi-tenant deployments ([#355](https://github.com/adcontextprotocol/adcp-client-python/issues/355)) ([#368](https://github.com/adcontextprotocol/adcp-client-python/issues/368)) ([1e27dad](https://github.com/adcontextprotocol/adcp-client-python/commit/1e27dad86787cfa09155e937fa0c3b2e7d169911))
* **server:** synthesize host:* siblings for bare allowed_hosts ([#537](https://github.com/adcontextprotocol/adcp-client-python/issues/537)) ([2733423](https://github.com/adcontextprotocol/adcp-client-python/commit/2733423bb8286ee809f9003804de096ce9fd9e0f))
* **server:** transport="both" — host MCP and A2A on a single binary ([#354](https://github.com/adcontextprotocol/adcp-client-python/issues/354)) ([#370](https://github.com/adcontextprotocol/adcp-client-python/issues/370)) ([1d5d907](https://github.com/adcontextprotocol/adcp-client-python/commit/1d5d9074bc4868a6573fe1e2d862a660767dfab7))
* **signing:** async_resolve_agent — bootstrap to JWKS via brand.json ([#389](https://github.com/adcontextprotocol/adcp-client-python/issues/389)) ([0d78bd1](https://github.com/adcontextprotocol/adcp-client-python/commit/0d78bd137391207313281f5abf0f364c5200e85e))
* **signing:** v3-identity Tier 1 — BrandJsonJwksResolver + CapabilityCache (port from JS) ([#345](https://github.com/adcontextprotocol/adcp-client-python/issues/345)) ([a016eca](https://github.com/adcontextprotocol/adcp-client-python/commit/a016eca80bfa9ee3db90d62045a847cb29004c8a))
* **signing:** verify_from_agent_url — single-call resolver+verifier factory ([#401](https://github.com/adcontextprotocol/adcp-client-python/issues/401)) ([5931921](https://github.com/adcontextprotocol/adcp-client-python/commit/5931921a483fa2b1126e13c71da2e882812a16f7))
* **storyboards:** require full proposal_finalize chain after @adcp/sdk@6.10.0 ([#565](https://github.com/adcontextprotocol/adcp-client-python/issues/565)) ([b1fe9d5](https://github.com/adcontextprotocol/adcp-client-python/commit/b1fe9d572253cab28ce6362d0bee7abb8ffb7fed))
* **testing:** add build_test_client async context manager ([#554](https://github.com/adcontextprotocol/adcp-client-python/issues/554)) ([5f038e5](https://github.com/adcontextprotocol/adcp-client-python/commit/5f038e5cc442173ed3e2726e38f856610d898473))
* **testing:** make_request_context + build_asgi_app helpers ([#535](https://github.com/adcontextprotocol/adcp-client-python/issues/535)) ([6869976](https://github.com/adcontextprotocol/adcp-client-python/commit/68699763e917980f6d7ad75e4a1364a2dc188715))
* **testing:** storyboard runner matrix entry for v3 reference seller ([#410](https://github.com/adcontextprotocol/adcp-client-python/issues/410)) ([#426](https://github.com/adcontextprotocol/adcp-client-python/issues/426)) ([f047c23](https://github.com/adcontextprotocol/adcp-client-python/commit/f047c23616250dc12631a953e9c0f0b65b502af6))
* **types,decisioning:** close salesagent migration export gaps ([#511](https://github.com/adcontextprotocol/adcp-client-python/issues/511)) ([97a52e8](https://github.com/adcontextprotocol/adcp-client-python/commit/97a52e8412319e1693415af21dec47fcf788b3fd))
* **types:** Account v3 projection helpers — bank-details write-only guard ([#371](https://github.com/adcontextprotocol/adcp-client-python/issues/371)) ([e4415e3](https://github.com/adcontextprotocol/adcp-client-python/commit/e4415e3f7aec181100b8b2581d71e0a54d1a4728))
* **types:** narrow discriminated-union errors (Stability AI Emma P2) ([#340](https://github.com/adcontextprotocol/adcp-client-python/issues/340)) ([ad8b520](https://github.com/adcontextprotocol/adcp-client-python/commit/ad8b5206c6ef7eac088f335b00902a16c4eacd3d))
* **v3-ref-seller:** broaden sales surface + sync_accounts + invoice_recipient ([#408](https://github.com/adcontextprotocol/adcp-client-python/issues/408)) ([f1e325b](https://github.com/adcontextprotocol/adcp-client-python/commit/f1e325bd9682e2792d9234ceb5adb31b8d77ab48))
* **v3-ref-seller:** enable server validation + declare account.supported_billing ([#402](https://github.com/adcontextprotocol/adcp-client-python/issues/402)) ([ac7a61f](https://github.com/adcontextprotocol/adcp-client-python/commit/ac7a61fae0f41b2bf51b5e560edc118437f07638))
* **v3-ref-seller:** translator pattern with JS mock-server upstream ([#447](https://github.com/adcontextprotocol/adcp-client-python/issues/447)) ([4185ced](https://github.com/adcontextprotocol/adcp-client-python/commit/4185cedce4cb27dbe6cfc0e104398d6811a48ccc))
* **validation:** ADCP_VALIDATION_MODE env var + outputSchema on tools/list ([#391](https://github.com/adcontextprotocol/adcp-client-python/issues/391)) ([fa35bfb](https://github.com/adcontextprotocol/adcp-client-python/commit/fa35bfb5f4a17d97df6ce038e8b15cb54e88334e))
* **validation:** oneOf near-miss validator hints + issues[].hint on every VALIDATION_ERROR ([#476](https://github.com/adcontextprotocol/adcp-client-python/issues/476)) ([9985086](https://github.com/adcontextprotocol/adcp-client-python/commit/9985086c23cbab80cf67348e0c84177f749f733f))
* **webhook:** PgWebhookDeliverySupervisor — Postgres-backed multi-worker delivery ([#360](https://github.com/adcontextprotocol/adcp-client-python/issues/360)) ([5f1f3a0](https://github.com/adcontextprotocol/adcp-client-python/commit/5f1f3a0fca2162547dda0c24c6f4723daced6b0b))
* **webhooks:** HMAC/bearer/Docker/Standard Webhooks delivery modes ([#482](https://github.com/adcontextprotocol/adcp-client-python/issues/482)) ([5d2c220](https://github.com/adcontextprotocol/adcp-client-python/commit/5d2c2208016b2355fbd0350a9ce039398abffb8d))


### Bug Fixes

* **auth:** populate ctx.auth_principal from bearer ContextVar ([#574](https://github.com/adcontextprotocol/adcp-client-python/issues/574)) ([6c49b31](https://github.com/adcontextprotocol/adcp-client-python/commit/6c49b319b3a994eee475f45c8122b96bc10cf984))
* **ci:** add --extra dev to pre-commit mypy hook so types-protobuf stubs are present ([#431](https://github.com/adcontextprotocol/adcp-client-python/issues/431)) ([254d0e2](https://github.com/adcontextprotocol/adcp-client-python/commit/254d0e2173c300598919e9fc1203fe141e934e7c))
* **ci:** add concurrency group to cancel superseded runs on force-push ([#444](https://github.com/adcontextprotocol/adcp-client-python/issues/444)) ([272fb01](https://github.com/adcontextprotocol/adcp-client-python/commit/272fb0106a1b85ab40f5621e7dcd209a4cffca10)), closes [#413](https://github.com/adcontextprotocol/adcp-client-python/issues/413)
* **ci:** align pre-commit mypy with CI — install mypy in uv dev group ([#427](https://github.com/adcontextprotocol/adcp-client-python/issues/427)) ([d3d8f7b](https://github.com/adcontextprotocol/adcp-client-python/commit/d3d8f7bb048b9c328550003d10aa4705f6e21a93))
* **ci:** pre-install @adcp/client + bump readiness window for v3 storyboard ([#448](https://github.com/adcontextprotocol/adcp-client-python/issues/448)) ([6b89599](https://github.com/adcontextprotocol/adcp-client-python/commit/6b89599ca25a948f378c2df214e8d4a7c8f6791f))
* **decisioning:** address expert review — major_versions passthrough, supported_protocols default, nested re-exports ([b72b744](https://github.com/adcontextprotocol/adcp-client-python/commit/b72b74485a5fb1c6800bc2659e0ec22740c90fc0)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **decisioning:** apply 3.10 mock-target fix to second compliance_testing test ([d086aa8](https://github.com/adcontextprotocol/adcp-client-python/commit/d086aa88d8a709a7e8a5b2876b9079dba540fc8f)), closes [#485](https://github.com/adcontextprotocol/adcp-client-python/issues/485)
* **decisioning:** fire legacy-field DeprecationWarnings at construction ([679709b](https://github.com/adcontextprotocol/adcp-client-python/commit/679709b1a269cbe4510264dad0c8bb8588ab48ee)), closes [#485](https://github.com/adcontextprotocol/adcp-client-python/issues/485)
* **decisioning:** handle Specialism|str union in create_media_buy_store ([6f3faa0](https://github.com/adcontextprotocol/adcp-client-python/commit/6f3faa0447c328ca82ee29e03e52ee6a4f2a5140)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **decisioning:** handler shims for every non-sales wire tool ([#337](https://github.com/adcontextprotocol/adcp-client-python/issues/337)) ([a51c5a4](https://github.com/adcontextprotocol/adcp-client-python/commit/a51c5a49d99132a51f1b95721f5d4329110fe164))
* **decisioning:** mock target Python 3.10 import-resolution compat ([40b4517](https://github.com/adcontextprotocol/adcp-client-python/commit/40b4517f57054e3fa1115c1e8d2d0a4e1d2abdcc)), closes [#485](https://github.com/adcontextprotocol/adcp-client-python/issues/485)
* **decisioning:** post-[#484](https://github.com/adcontextprotocol/adcp-client-python/issues/484) capabilities-projection polish (closes [#485](https://github.com/adcontextprotocol/adcp-client-python/issues/485)) ([87bacfb](https://github.com/adcontextprotocol/adcp-client-python/commit/87bacfbe708b5eca8f56b151572541c934bb7f97))
* **decisioning:** TaskHandoff registry.fail/complete echo request context ([#569](https://github.com/adcontextprotocol/adcp-client-python/issues/569)) ([b73b899](https://github.com/adcontextprotocol/adcp-client-python/commit/b73b8999c229287a5c1ba3364b17b4233c2aa438))
* **decisioning:** three Emma cross-cutting findings + matrix runner ([#341](https://github.com/adcontextprotocol/adcp-client-python/issues/341)) ([4938ee1](https://github.com/adcontextprotocol/adcp-client-python/commit/4938ee1f913c37ecd5ac3b6f2c9a368b0364e405))
* **decisioning:** Tier 2 codes → spec-conformant PERMISSION_DENIED ([#375](https://github.com/adcontextprotocol/adcp-client-python/issues/375)) ([#393](https://github.com/adcontextprotocol/adcp-client-python/issues/393)) ([870e98a](https://github.com/adcontextprotocol/adcp-client-python/commit/870e98ad6657a0dd2214d695765e3b5bc6b7228b))
* **decisioning:** Tier 2 expert-review fix-pack ([#372](https://github.com/adcontextprotocol/adcp-client-python/issues/372)) ([9605f44](https://github.com/adcontextprotocol/adcp-client-python/commit/9605f44518c79c544d3ff1539395234c3f8b73a8))
* **decisioning:** update v3 reference seller smoke tests for Specialism|str union ([09c5669](https://github.com/adcontextprotocol/adcp-client-python/commit/09c5669d1f1f9669adc3a21b47c47e63169528ab)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **decisioning:** wire-path dispatch + F12 silent no-op (Emma sales-direct P0s) ([#338](https://github.com/adcontextprotocol/adcp-client-python/issues/338)) ([50c985e](https://github.com/adcontextprotocol/adcp-client-python/commit/50c985eda951fa9a720ad9c5ff0bb758efdd812f))
* **examples/multi_platform_seller:** align mocks with wire contract; storyboard gate now blocking ([#508](https://github.com/adcontextprotocol/adcp-client-python/issues/508)) ([863bb93](https://github.com/adcontextprotocol/adcp-client-python/commit/863bb933f88d12b987bc64aeff109636986c639a))
* **examples/multi_platform_seller:** list_creatives populates query_summary (closes [#510](https://github.com/adcontextprotocol/adcp-client-python/issues/510)) ([#521](https://github.com/adcontextprotocol/adcp-client-python/issues/521)) ([c7cfe6f](https://github.com/adcontextprotocol/adcp-client-python/commit/c7cfe6fb05622cb68fb194f24dfef4dea8fc317b))
* **idempotency:** @IdempotencyStore.wrap supports arg-projected methods ([#567](https://github.com/adcontextprotocol/adcp-client-python/issues/567)) ([1abdae6](https://github.com/adcontextprotocol/adcp-client-python/commit/1abdae676c8f3bcf2bef48ea5a6d192a5da7de92))
* **migrate:** per-symbol replacement for generated_poc reach-ins ([#329](https://github.com/adcontextprotocol/adcp-client-python/issues/329)) ([100680b](https://github.com/adcontextprotocol/adcp-client-python/commit/100680beba6ea99d5bca606d4582c4e7b6559d34))
* **server:** BearerTokenAuthMiddleware now populates ctx.auth_info for bearer flows ([#579](https://github.com/adcontextprotocol/adcp-client-python/issues/579)) ([901aa53](https://github.com/adcontextprotocol/adcp-client-python/commit/901aa53e6b86b3d8bde92ff4a434cd19dbc9f1f3))
* **server:** echo request context on AdcpError error envelopes ([#560](https://github.com/adcontextprotocol/adcp-client-python/issues/560)) ([2e351b0](https://github.com/adcontextprotocol/adcp-client-python/commit/2e351b0d20c73c8483deeaa4c0efa89d3c453af7))
* **server:** protocol-polish follow-ups from PR [#341](https://github.com/adcontextprotocol/adcp-client-python/issues/341) ([#342](https://github.com/adcontextprotocol/adcp-client-python/issues/342)) ([5a37adc](https://github.com/adcontextprotocol/adcp-client-python/commit/5a37adca7364f8c0892d991540444caf024eda0c))
* **testing:** make a2a_compat_shim resilient to wrong a2a-sdk in /tmp worktrees ([#433](https://github.com/adcontextprotocol/adcp-client-python/issues/433)) ([c5c581d](https://github.com/adcontextprotocol/adcp-client-python/commit/c5c581dedb591d37565f52fff69d5f5e13619164))
* **types:** CreateMediaBuy handler return type covers all 3 branches + add Submitted alias ([#575](https://github.com/adcontextprotocol/adcp-client-python/issues/575)) ([f7960ed](https://github.com/adcontextprotocol/adcp-client-python/commit/f7960ed5778e6d96299eaa7246afa8a03682de9f))


### Performance Improvements

* **ci:** cache ~/.npm + drop per-invocation npx for storyboard runners ([#450](https://github.com/adcontextprotocol/adcp-client-python/issues/450)) ([386cb15](https://github.com/adcontextprotocol/adcp-client-python/commit/386cb15071673a7340a54b31db07bd07ac448300))
* **server:** lazy-load Pydantic schema generation to fix storyboard readiness flake ([#435](https://github.com/adcontextprotocol/adcp-client-python/issues/435)) ([8664ea3](https://github.com/adcontextprotocol/adcp-client-python/commit/8664ea37a4468646a1caef3238d7eb3d3b28a9f5))


### Documentation

* **adcp:** document git worktree isolation pattern for parallel agents ([#428](https://github.com/adcontextprotocol/adcp-client-python/issues/428)) ([117d4f2](https://github.com/adcontextprotocol/adcp-client-python/commit/117d4f2f4bd1979123d91d4e590aebab8969b814))
* **adcp:** fix a2a-sdk 1.0.x symbol guidance in v3→v4 migration guide ([#524](https://github.com/adcontextprotocol/adcp-client-python/issues/524)) ([57cb4e5](https://github.com/adcontextprotocol/adcp-client-python/commit/57cb4e51261e5b929d056005ed6138fbc8d5722d)), closes [#514](https://github.com/adcontextprotocol/adcp-client-python/issues/514)
* **agents:** add parallel agent coordination protocol to CLAUDE.md ([#430](https://github.com/adcontextprotocol/adcp-client-python/issues/430)) ([abf0d65](https://github.com/adcontextprotocol/adcp-client-python/commit/abf0d65563f3cde45d907950214fbd16d3a2857f))
* **decisioning:** adopter guide for declaring capabilities ([f2e8f9a](https://github.com/adcontextprotocol/adcp-client-python/commit/f2e8f9acbe598ed4b9b02fe5afebdc3fac80bd7a)), closes [#479](https://github.com/adcontextprotocol/adcp-client-python/issues/479)
* **examples:** Alembic migration scaffold for v3 reference seller ([#390](https://github.com/adcontextprotocol/adcp-client-python/issues/390)) ([3035c25](https://github.com/adcontextprotocol/adcp-client-python/commit/3035c25ba906680facb685c66df784cab6d3de49))
* **examples:** expand hello_seller.py to full 9-method sales-non-guaranteed surface ([#531](https://github.com/adcontextprotocol/adcp-client-python/issues/531)) ([7d21302](https://github.com/adcontextprotocol/adcp-client-python/commit/7d213026414038e4a92e4318ba8a3c9194e04ea1))
* **examples:** webhook wiring recipe — handler-authoring section + expanded hello_seller + new with_webhooks example ([#551](https://github.com/adcontextprotocol/adcp-client-python/issues/551)) ([6cf71cb](https://github.com/adcontextprotocol/adcp-client-python/commit/6cf71cbb7fed2169f43c847fc28f5685c29a995f))
* **migration:** anchor v3→v4 adopter numbers to v4.0 release, point to live codemod ([#522](https://github.com/adcontextprotocol/adcp-client-python/issues/522)) ([11732c8](https://github.com/adcontextprotocol/adcp-client-python/commit/11732c87bd494874801ec55ae139634a7a604fa6))
* **proposals:** product architecture — layered model + two-platform composition ([#502](https://github.com/adcontextprotocol/adcp-client-python/issues/502)) ([69c585c](https://github.com/adcontextprotocol/adcp-client-python/commit/69c585c962c2e531fd7d5d76d9be0a326ca7eea7))
* **proposals:** ProposalManager v1.5 design — session cache, finalize, expires_at, capability validation, recipe lifecycle ([#538](https://github.com/adcontextprotocol/adcp-client-python/issues/538)) ([8ea85ad](https://github.com/adcontextprotocol/adcp-client-python/commit/8ea85ad992b5571b84c88ab7b5a24e824339e575))
* **proposals:** when to use which proposal surface ([#533](https://github.com/adcontextprotocol/adcp-client-python/issues/533)) ([a9fd18a](https://github.com/adcontextprotocol/adcp-client-python/commit/a9fd18aa8aba806204a054c315797ea8da1beb9e))

## [4.3.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.2.0...v4.3.0) (2026-04-30)


### Features

* **server:** public register_handler_tools seam + advertised_tools class attr ([#318](https://github.com/adcontextprotocol/adcp-client-python/issues/318)) ([d96822a](https://github.com/adcontextprotocol/adcp-client-python/commit/d96822a64b54289c23e143b39d5e95808b65c1c2))
* **signing:** SigningProvider Protocol for KMS-backed signing ([#283](https://github.com/adcontextprotocol/adcp-client-python/issues/283)) ([#323](https://github.com/adcontextprotocol/adcp-client-python/issues/323)) ([4de648e](https://github.com/adcontextprotocol/adcp-client-python/commit/4de648e27f0df522269e9cfb28a5b40aa8138527))


### Bug Fixes

* **examples:** close last 5 storyboard fixture-dependent failures ([#319](https://github.com/adcontextprotocol/adcp-client-python/issues/319)) ([#322](https://github.com/adcontextprotocol/adcp-client-python/issues/322)) ([d92cfb1](https://github.com/adcontextprotocol/adcp-client-python/commit/d92cfb13839fa26a4ab69ae2db941c752db054a1))
* **examples:** seed_product complete defaults + format_ids agent_url normalization ([#319](https://github.com/adcontextprotocol/adcp-client-python/issues/319)) ([#321](https://github.com/adcontextprotocol/adcp-client-python/issues/321)) ([046b15e](https://github.com/adcontextprotocol/adcp-client-python/commit/046b15e429b40412a284d50e9d28bf884355c80f))


### Documentation

* **handler-authoring:** expand with salesagent migration production patterns ([#326](https://github.com/adcontextprotocol/adcp-client-python/issues/326)) ([ce4c5df](https://github.com/adcontextprotocol/adcp-client-python/commit/ce4c5df50fa156f3ff318c6a4c6376019adff540))

## [4.2.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.1.0...v4.2.0) (2026-04-30)


### Features

* **examples:** DemoStore overrides for force_create_media_buy_arm, force_task_completion, and seed_* scenarios ([#313](https://github.com/adcontextprotocol/adcp-client-python/issues/313)) ([e2a2707](https://github.com/adcontextprotocol/adcp-client-python/commit/e2a270778960a891cdb8afa8d30d00e3570afd4d))
* **sdk:** per-instance adcp_version pin + wire emission (Stage 2 + 3a) ([#294](https://github.com/adcontextprotocol/adcp-client-python/issues/294)) ([4aa7a6d](https://github.com/adcontextprotocol/adcp-client-python/commit/4aa7a6d1a19a6ff681b13d773e55cfb09bab495f))
* **server:** add force_create_media_buy_arm + force_task_completion controller scenarios ([#282](https://github.com/adcontextprotocol/adcp-client-python/issues/282)) ([9818250](https://github.com/adcontextprotocol/adcp-client-python/commit/9818250c75bc08deaf43af5c829c78eabb1cd342))
* **test-controller:** add seed_creative_format scenario; advertise force_session_status in capabilities ([#315](https://github.com/adcontextprotocol/adcp-client-python/issues/315)) ([ce7e9ab](https://github.com/adcontextprotocol/adcp-client-python/commit/ce7e9ab2c9569a69a39601b224278c0b7a349b1e))


### Bug Fixes

* **examples:** seller_agent.py AdCP 3.0.1 storyboard compliance (items 1-6 of [#304](https://github.com/adcontextprotocol/adcp-client-python/issues/304)) ([#310](https://github.com/adcontextprotocol/adcp-client-python/issues/310)) ([04966d7](https://github.com/adcontextprotocol/adcp-client-python/commit/04966d7183811238d57e28a3aebc63e6de135a32))
* **server:** comply_test_controller returns dict to fix controller_detected: false ([#317](https://github.com/adcontextprotocol/adcp-client-python/issues/317)) ([9244d46](https://github.com/adcontextprotocol/adcp-client-python/commit/9244d464e756dcef969ebdb3f9b49369c06a1f58))
* **server:** fix streamable-http ASGI error, host binding, and AdCP 3.0.1 scenario gaps ([#296](https://github.com/adcontextprotocol/adcp-client-python/issues/296)) ([6be0232](https://github.com/adcontextprotocol/adcp-client-python/commit/6be02328be979ccd32ac2914d8b5e04c0923dd02))

## [4.1.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v4.0.0...v4.1.0) (2026-04-30)


### ⚠ BREAKING CHANGES

* **migration:** ``FormatId.__name__`` and ``__qualname__`` change from ``"FormatId"`` to ``"FormatReferenceStructuredObject"`` because AdCP 3.0.1 polished the schema title on ``core/format-id.json``. The public ``adcp.FormatId`` alias keeps working — ``Format(format_id= FormatId(...))`` and ``isinstance(x, FormatId)`` are unchanged. Two niche cases break: pickled ``FormatId`` instances from 4.0 fail to unpickle on 4.1, and snapshot tests / log scrapers asserting on ``__name__`` see the rename. See MIGRATION_v4.0_to_v4.1.md.
* **a2a:** `ADCPClient.pending_task_id` is now `ADCPClient.active_task_id` (same for `A2AAdapter`). The constructor's `context_id=` kwarg and `reset_context()` now raise `TypeError` (was `ValueError`) on non-A2A protocols — the string value is fine, the operation doesn't apply to MCP.

### Features

* **a2a:** checkpoint/from_checkpoint API, harden context-id retention ([a17ffb3](https://github.com/adcontextprotocol/adcp-client-python/commit/a17ffb382aa1d777e5e98fdc26c6cc9efd4898c0))
* **a2a:** expose peer protocol versions, add force_a2a_version pin ([3898c53](https://github.com/adcontextprotocol/adcp-client-python/commit/3898c536fb08dd088ff1074216a8c56ae727dc63))
* **a2a:** migrate to a2a-sdk 1.0 with 0.3 wire-compat shim ([c07db7d](https://github.com/adcontextprotocol/adcp-client-python/commit/c07db7df96b1744b0660ecf367375178ef87defc))
* **a2a:** migrate to a2a-sdk 1.0 with 0.3 wire-compat shim (Release-As: 4.1.0) ([28a4a13](https://github.com/adcontextprotocol/adcp-client-python/commit/28a4a13867adb039504fde59ceab48e2d961380a))
* **adcp:** support ADCP_BASE_URL env override in sync_schemas.py ([#285](https://github.com/adcontextprotocol/adcp-client-python/issues/285)) ([a2bc977](https://github.com/adcontextprotocol/adcp-client-python/commit/a2bc9770dfb86a5b2cca94b671af8cb899224ce5))
* **adcp:** sync canonical agent skills from protocol tarball ([#275](https://github.com/adcontextprotocol/adcp-client-python/issues/275)) ([5312f05](https://github.com/adcontextprotocol/adcp-client-python/commit/5312f05df25d8b6ad240f497e88a4c15f58990a8))
* **agents:** /claude-triage comment nudge ([5af8b47](https://github.com/adcontextprotocol/adcp-client-python/commit/5af8b4736516b986045717d62da94e700aa57cca))
* **agents:** /claude-triage comment nudge ([d3b8b28](https://github.com/adcontextprotocol/adcp-client-python/commit/d3b8b28d8dc9cbf1ab2df86b1a0c7f619ad3c332))
* **agents:** add Claude Code routines scaffolding ([aa23ef3](https://github.com/adcontextprotocol/adcp-client-python/commit/aa23ef3d35de8b7740924b4ae1ec6ec7b8a832a7))
* **agents:** add Claude Code routines scaffolding ([d312abf](https://github.com/adcontextprotocol/adcp-client-python/commit/d312abff8e63af7a36b23073e33d8976dd9214aa))
* **agents:** add silent-triage path to triage prompt ([6abb495](https://github.com/adcontextprotocol/adcp-client-python/commit/6abb49529d79a3947fd95c675c9f48852c8adff3))
* **agents:** commit .claude/agents/ experts for cloud routine access ([55144f2](https://github.com/adcontextprotocol/adcp-client-python/commit/55144f273513479ba64f3c95682a84ef69847815))
* **agents:** migrate manual triage nudge to /triage slash-command-dispatch ([#267](https://github.com/adcontextprotocol/adcp-client-python/issues/267)) ([6bd434d](https://github.com/adcontextprotocol/adcp-client-python/commit/6bd434d488f480eb75f8dba075a22fcfa544d44a))
* **agents:** security + product review fixes for triage routine ([cbafef5](https://github.com/adcontextprotocol/adcp-client-python/commit/cbafef5c76d2c59328139e743194c216a5dc5d51))
* **agents:** switch triage nudge prefix to [@claude-triage](https://github.com/claude-triage) ([#265](https://github.com/adcontextprotocol/adcp-client-python/issues/265)) ([77bfce2](https://github.com/adcontextprotocol/adcp-client-python/commit/77bfce298e7fc879ea409dc97f6975e72511dd1e))
* **agents:** triage bundles ready items, never splits issues ([#266](https://github.com/adcontextprotocol/adcp-client-python/issues/266)) ([2227a6b](https://github.com/adcontextprotocol/adcp-client-python/commit/2227a6bb23e4c7486388c3663f9b87611d39914f))
* **agents:** triage default — execute when outcome is clear ([e3f8a6c](https://github.com/adcontextprotocol/adcp-client-python/commit/e3f8a6c8787f623881d3b3a2c9e8df43b126c4c5))
* **agents:** triage defer subtypes + partial-rollout linkage rule ([#273](https://github.com/adcontextprotocol/adcp-client-python/issues/273)) ([c97aa95](https://github.com/adcontextprotocol/adcp-client-python/commit/c97aa95e1e9e8325802a71f92f807306128556e5))
* **agents:** triage fires on comments + claude-triaging lifecycle label ([#277](https://github.com/adcontextprotocol/adcp-client-python/issues/277)) ([604d795](https://github.com/adcontextprotocol/adcp-client-python/commit/604d79587be99af4a08e815c1dddc527d3b36721))
* **agents:** triage PR ergonomics — refs adcp[#3121](https://github.com/adcontextprotocol/adcp-client-python/issues/3121) ([#279](https://github.com/adcontextprotocol/adcp-client-python/issues/279)) ([31606eb](https://github.com/adcontextprotocol/adcp-client-python/commit/31606eb2230267fe9de7c5149ac716af239ff33a))
* **agents:** triage prompt handles RFC/epic/scope bucket/milestone ([cac3525](https://github.com/adcontextprotocol/adcp-client-python/commit/cac35250b8a57a978bb481aa5134f38d1731fe5f))
* **agents:** triage runs pre-PR build+test gate before expert review ([#271](https://github.com/adcontextprotocol/adcp-client-python/issues/271)) ([8fcc5ee](https://github.com/adcontextprotocol/adcp-client-python/commit/8fcc5ee220a0f4792fec9b724e7b602b99596024))
* **agents:** triage runs pre-PR expert review on diff before opening PR ([#269](https://github.com/adcontextprotocol/adcp-client-python/issues/269)) ([46dd708](https://github.com/adcontextprotocol/adcp-client-python/commit/46dd708f7dcc6a241b655745ab281001b6a1dbc7))
* **agents:** triage v2 — expert consultation + race + coverage ([bf6e18b](https://github.com/adcontextprotocol/adcp-client-python/commit/bf6e18bdacebdf69f959bb7c915ef34cd6333a13))
* **client:** ADCPClient.from_mcp_client() factory for in-process MCP transport ([#293](https://github.com/adcontextprotocol/adcp-client-python/issues/293)) ([d83aa53](https://github.com/adcontextprotocol/adcp-client-python/commit/d83aa53981c80b9e50a84b5a745e85d5837e0d1a))
* **signing:** close 4 SSRF gaps and add opt-in port hardening (foundation audit) ([84b837e](https://github.com/adcontextprotocol/adcp-client-python/commit/84b837e0e093dff041254781bb12b4a416150bd2))
* **signing:** default replay store, signed-fetch preset, migration guide ([#272](https://github.com/adcontextprotocol/adcp-client-python/issues/272)) ([52019b8](https://github.com/adcontextprotocol/adcp-client-python/commit/52019b8942383ca9ec79a4bd4c0f7579f7aa1ffc))
* **signing:** drain three foundation-audit deferreds ([#298](https://github.com/adcontextprotocol/adcp-client-python/issues/298), [#299](https://github.com/adcontextprotocol/adcp-client-python/issues/299), [#300](https://github.com/adcontextprotocol/adcp-client-python/issues/300)) ([072998a](https://github.com/adcontextprotocol/adcp-client-python/commit/072998a8fd7b33097b7e6ab0496c2ecadf33a691))
* **validation:** schema-driven validation with client hooks and opt-in server middleware ([5cbac87](https://github.com/adcontextprotocol/adcp-client-python/commit/5cbac871e222eaf037346632fd4ddf1354bfd6ee))
* **validation:** schema-driven validation with client hooks and opt-in server middleware ([a38ff57](https://github.com/adcontextprotocol/adcp-client-python/commit/a38ff57c934eded647ac84a470bb79a4a1b8bca6))


### Bug Fixes

* **a2a:** address expert-review feedback on 1.0 migration ([7e54b97](https://github.com/adcontextprotocol/adcp-client-python/commit/7e54b97859321b8c9ed43bd87e899820cafcfc3d))
* **adcp:** skip eager httpx.AsyncClient alloc in WebhookSender.__aenter__ on owned-client path ([#301](https://github.com/adcontextprotocol/adcp-client-python/issues/301)) ([4bd45d1](https://github.com/adcontextprotocol/adcp-client-python/commit/4bd45d1c059ceeddfd184793a3f477f915c98446)), closes [#300](https://github.com/adcontextprotocol/adcp-client-python/issues/300)
* **agents:** already-engaged check + tighten label creation ([02baa89](https://github.com/adcontextprotocol/adcp-client-python/commit/02baa891ad0ccfd6ec392ae0350dd07671d65a0c))
* **agents:** triage already-engaged + ship-more (missed in [#259](https://github.com/adcontextprotocol/adcp-client-python/issues/259)) ([ca42d29](https://github.com/adcontextprotocol/adcp-client-python/commit/ca42d2911bc07108e726bc6227e666ff97eb6724))
* **agents:** webhook-miss sweep grace period (no double-fire) ([#280](https://github.com/adcontextprotocol/adcp-client-python/issues/280)) ([3a5f1c2](https://github.com/adcontextprotocol/adcp-client-python/commit/3a5f1c2a25f59ae485f90a5aa47c3e7119ad11ad))
* **deps:** add types-protobuf to pip dev extras ([801ac16](https://github.com/adcontextprotocol/adcp-client-python/commit/801ac1688309497d71823163098a7449e5c082e4))
* **handlers:** sync MEDIA_BUY_STATE_MACHINE with spec v3 enum ([#289](https://github.com/adcontextprotocol/adcp-client-python/issues/289)) ([54fd18b](https://github.com/adcontextprotocol/adcp-client-python/commit/54fd18b45cbef587db0d6ce48531c5b35e3c302b))
* **signing:** validate-before-sign symmetry in deliver() + HMAC SSRF coverage + 4.1 migration notes ([bc8da3a](https://github.com/adcontextprotocol/adcp-client-python/commit/bc8da3aa52a8eae61fab20da6a57359dfb96a2f2))
* **triage:** drop apostrophe from MODE text (port of adcp[#3325](https://github.com/adcontextprotocol/adcp-client-python/issues/3325)) ([#287](https://github.com/adcontextprotocol/adcp-client-python/issues/287)) ([2efa423](https://github.com/adcontextprotocol/adcp-client-python/commit/2efa4238006b2feccfa253099d1cc6bf4dd07b84))
* **validation:** address code + security review feedback ([5bf9434](https://github.com/adcontextprotocol/adcp-client-python/commit/5bf9434da477140768a9aa55cde493e89adfcf0e))


### Documentation

* **migration:** add v4.0 → v4.1 migration guide ([#302](https://github.com/adcontextprotocol/adcp-client-python/issues/302)) ([01a1068](https://github.com/adcontextprotocol/adcp-client-python/commit/01a106891df33e8409bf95a63f11eec08ebbcf15))
* **readme:** note AAO IPR Policy requirement for contributors ([43d77f9](https://github.com/adcontextprotocol/adcp-client-python/commit/43d77f9d4e1fc35bd6579f985f7907fbb430ed25))
* **readme:** note AAO IPR Policy requirement for contributors ([ba03ecb](https://github.com/adcontextprotocol/adcp-client-python/commit/ba03ecbf3e6c3132003a6646af9ad623779c5e92))

## [4.0.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.12.0...v4.0.0) (2026-04-22)


### ⚠ BREAKING CHANGES

* **types:** rename <Type>Asset content types to <Type>Content
* **server:** pluggable TaskStore on A2A — unblock production A2A adoption
* **server:** expert-review followups — tenant-scoped idempotency, A2A context_factory, ContextVar safety
* **types:** AssetsNN semantic aliases + format_category shim + downstream smoke
* **sdk:** ResolvedBrand.brand_manifest removed. Use .brand. CreateMediaBuyRequest.brand_manifest removed. Use brand. BrandManifest, FormatCategory, DeliverTo, PromotedProducts, PromotedOfferings, Pricing, PackageStatus imports now raise ImportError. See MIGRATION_v3_to_v4.md.
* **webhooks:** ADCPClient webhook verification now requires raw_body to be passed through from the HTTP handler. Callers that relied on the implicit re-serialize-from-payload fallback will start seeing ADCPWebhookSignatureError until they plumb the raw body through from their framework's pre-parse hook. Fix path:
* **webhooks:** get_adcp_signed_headers_for_webhook now signs the compact-separator JSON form of the payload. Callers that previously hand-serialized spaced JSON and POSTed it with content= will see signature mismatches after this change. The fix is to also serialize with separators=(",", ":") or switch to httpx json= which already uses that form.
* serve(mount=...) kwarg removed.
* ``Budget.authority_level`` is removed. Migrate to ``reallocation_threshold`` / ``reallocation_unlimited`` on ``plan.budget``, and set ``plan.human_review_required`` for decisions affecting data subjects. See the rc.4 migration section in README.md.

### Features

* A2A server support in serve() ([d0c3015](https://github.com/adcontextprotocol/adcp-client-python/commit/d0c3015db1ff691964a1791414c3abedeaed225f))
* A2A server support in serve() ([4e5db4f](https://github.com/adcontextprotocol/adcp-client-python/commit/4e5db4f71839efc3db61f4c415e315f7c091dbbf)), closes [#175](https://github.com/adcontextprotocol/adcp-client-python/issues/175)
* **a2a:** auto-retain contextId + taskId across multi-turn calls ([0564635](https://github.com/adcontextprotocol/adcp-client-python/commit/056463547e88dde2919d644d2dbc32e13dea947a))
* **a2a:** auto-retain contextId + taskId across multi-turn calls ([8a084ec](https://github.com/adcontextprotocol/adcp-client-python/commit/8a084ecd1e8cf3e31c57fbe6f6c25f673b6cbe07))
* ADCP 3.0 server DX helpers, type guards, and schema sync ([de4a079](https://github.com/adcontextprotocol/adcp-client-python/commit/de4a079e6319994ca3a122815337be88be2c52e6))
* ADCP 3.0 server DX helpers, type guards, and schema sync to latest ([bf0d81d](https://github.com/adcontextprotocol/adcp-client-python/commit/bf0d81d4d2b399cb6a33c0b32562a798ca78bde4))
* AdCP RFC 9421 request-signing profile ([ba53961](https://github.com/adcontextprotocol/adcp-client-python/commit/ba5396181715ef514c6e0df11b675a516d24244b))
* add collection_list and sync_governance task methods ([2dbeac1](https://github.com/adcontextprotocol/adcp-client-python/commit/2dbeac1b6bb452a18674bf21ab67a547818b69ff))
* address PR review — real-world transforms and SDK error types ([cfaa9c5](https://github.com/adcontextprotocol/adcp-client-python/commit/cfaa9c505d7e82eed5c40f7651094e6dac703f91))
* AssetsNN aliases, format_category shim, MCP adoption hooks — unblock salesagent ([ed7d30a](https://github.com/adcontextprotocol/adcp-client-python/commit/ed7d30a8508b00c970247165191c0d9c83255d64))
* auto-inject context passthrough in create_tool_caller ([4d4fe5e](https://github.com/adcontextprotocol/adcp-client-python/commit/4d4fe5e556e66b4e1bd6f8543d6c6f4ce2f109ae))
* bump to 4.0.0b1 with test fixtures updated for new schema shape ([dcd6709](https://github.com/adcontextprotocol/adcp-client-python/commit/dcd670987d4853930e5f66773309cae2b4504880))
* bundle-based schema sync + Sigstore verify + 4.0.0b1 on latest ([755071a](https://github.com/adcontextprotocol/adcp-client-python/commit/755071a8e24f079c2e0b305a96d495fa654e362f))
* error translation helper for multi-transport servers ([7825ed6](https://github.com/adcontextprotocol/adcp-client-python/commit/7825ed60bc3ff0078c7050673bc169429e066487))
* error translation helper for multi-transport servers ([#176](https://github.com/adcontextprotocol/adcp-client-python/issues/176)) ([88126d1](https://github.com/adcontextprotocol/adcp-client-python/commit/88126d111c10d860d8e14d27d700e193c29b3c31))
* **examples:** add seller_agent.py reference impl ([40341c9](https://github.com/adcontextprotocol/adcp-client-python/commit/40341c90fea23df5a67ff3196bc68ed4026e3347))
* idempotency_key auto-injection, typed errors, and capability gate ([af9dd2d](https://github.com/adcontextprotocol/adcp-client-python/commit/af9dd2d18a5a78e500a8fdb9808f0c5c5c2c7f25))
* idempotency_key auto-injection, typed errors, and capability gate ([12cc983](https://github.com/adcontextprotocol/adcp-client-python/commit/12cc983b9126f4a816ffc69e2db45a84af169342))
* implement AdCP RFC 9421 request-signing profile ([fc995e9](https://github.com/adcontextprotocol/adcp-client-python/commit/fc995e9f084aecc910dc279d9d7b0bc64fe7737d))
* MCP response + error extraction per AdCP spec ([d991b1e](https://github.com/adcontextprotocol/adcp-client-python/commit/d991b1ef7bab7db596e721267ff396ef0bb99f45))
* MCP response + error extraction per AdCP spec ([896beb8](https://github.com/adcontextprotocol/adcp-client-python/commit/896beb8415d4142f25b9e6008ddbeedae17ed570))
* **mcp:** auto-generate sync_governance inputSchema from Pydantic ([ac1edb3](https://github.com/adcontextprotocol/adcp-client-python/commit/ac1edb3fda6f9848b95c40539c5bcfde03442cbe))
* **mcp:** inline \$defs in generated inputSchema (closes [#208](https://github.com/adcontextprotocol/adcp-client-python/issues/208)) ([8eaeb4d](https://github.com/adcontextprotocol/adcp-client-python/commit/8eaeb4d085d7fd3f5268a818d11e339eda950b26))
* **mcp:** inline $defs in generated inputSchema (closes [#208](https://github.com/adcontextprotocol/adcp-client-python/issues/208)) ([ffa58e5](https://github.com/adcontextprotocol/adcp-client-python/commit/ffa58e594130d400e3f300edead1ea04cb0361ca))
* **migrate+types:** v3-&gt;v4 codemod, strict-validation flag, version helpers, subclass test ([0f50d39](https://github.com/adcontextprotocol/adcp-client-python/commit/0f50d39ba6481f4a5bfcee11a82d16bfacb0f716))
* **migrate+types:** v3-&gt;v4 codemod, strict-validation flag, version helpers, subclass test ([32bfbeb](https://github.com/adcontextprotocol/adcp-client-python/commit/32bfbeb88f7be83e690adaf78a60aa753be88c25))
* salesagent review feedback - typed error codes, state machine export, missing types ([8ef8161](https://github.com/adcontextprotocol/adcp-client-python/commit/8ef8161bd51f886bea7eaeaf71bd010f60d1dc8c))
* **schemas:** regen for AdCP 3.0 GA — custom pricing + experimental_features ([4dfaffe](https://github.com/adcontextprotocol/adcp-client-python/commit/4dfaffe867cefb4e91183542ab0e5a03d451ee6c))
* **schemas:** regen for AdCP 3.0 GA — custom pricing + experimental_features ([db913ba](https://github.com/adcontextprotocol/adcp-client-python/commit/db913ba311ebba79e8401fe16e6674bb51bec9ef)), closes [#204](https://github.com/adcontextprotocol/adcp-client-python/issues/204)
* **sdk:** 4.0 beta cleanup — version wiring, brand_manifest drop, migration guide ([ebaab12](https://github.com/adcontextprotocol/adcp-client-python/commit/ebaab126db21877f27ceb5413ced02b315bc8dd3))
* **server+migrate:** round-1 feedback followups ([dbbc390](https://github.com/adcontextprotocol/adcp-client-python/commit/dbbc390fd30b78060e72108a01d45b09035e4cf6))
* **server+migrate:** round-1 feedback followups ([0fe9cb5](https://github.com/adcontextprotocol/adcp-client-python/commit/0fe9cb56daf01cd1cee6cb5fb16e3a94f51d03d4))
* **server:** AccountAwareToolContext + multi-tenant contract doc ([69fd3da](https://github.com/adcontextprotocol/adcp-client-python/commit/69fd3dab019c822d62fd1b4ac17b2628c3a36182))
* **server:** AccountAwareToolContext + multi-tenant contract doc ([3747939](https://github.com/adcontextprotocol/adcp-client-python/commit/3747939427e77b391bd9444beaa60c8b2f72e425))
* **server:** context_factory, tenant_id, DISCOVERY_TOOLS — unblock MCP adoption ([2203c1d](https://github.com/adcontextprotocol/adcp-client-python/commit/2203c1d162649096ee3d32886666d2d5e4ef0d4b))
* **server:** DISCOVERY_METHODS + document tools/list pre-auth posture (closes [#222](https://github.com/adcontextprotocol/adcp-client-python/issues/222)) ([a26c948](https://github.com/adcontextprotocol/adcp-client-python/commit/a26c94876226b17abee706c2e1df725af141d0c0))
* **server:** DISCOVERY_METHODS + lock tools/list pre-auth posture (closes [#222](https://github.com/adcontextprotocol/adcp-client-python/issues/222)) ([ba2de29](https://github.com/adcontextprotocol/adcp-client-python/commit/ba2de2964547f38105a9354a81a7c4bddad49762))
* **server:** gate tools/list on method overrides ([#220](https://github.com/adcontextprotocol/adcp-client-python/issues/220)) ([816d22c](https://github.com/adcontextprotocol/adcp-client-python/commit/816d22ccd809a0e9cf45c05789eea7e0c8178464))
* **server:** gate tools/list on method overrides (closes [#220](https://github.com/adcontextprotocol/adcp-client-python/issues/220)) ([fc1a788](https://github.com/adcontextprotocol/adcp-client-python/commit/fc1a7885325d3587a1066140c4838f4e46f8b9c8))
* **server:** idempotency middleware per AdCP [#2315](https://github.com/adcontextprotocol/adcp-client-python/issues/2315) spec ([101713c](https://github.com/adcontextprotocol/adcp-client-python/commit/101713c231598e07175b5350c98acb404c72d3ea))
* **server:** idempotency middleware per AdCP [#2315](https://github.com/adcontextprotocol/adcp-client-python/issues/2315) spec ([f708ed2](https://github.com/adcontextprotocol/adcp-client-python/commit/f708ed2312f13a1d8a82014901a0b4b2223342fe))
* **server:** middleware parity, auth, A2A parser hook, startup log ([549d190](https://github.com/adcontextprotocol/adcp-client-python/commit/549d190cf4ed9b82611f14e2a432d9dd43b870c3))
* **server:** middleware parity, auth, A2A parser hook, startup log ([c9d7bbc](https://github.com/adcontextprotocol/adcp-client-python/commit/c9d7bbc14717e0710cc848cf74f11445b4d3f870))
* **server:** per-skill middleware hook in ADCPAgentExecutor ([#226](https://github.com/adcontextprotocol/adcp-client-python/issues/226)) ([4e95764](https://github.com/adcontextprotocol/adcp-client-python/commit/4e95764984f54498f16699f8d7ed288c4e77bc3b))
* **server:** per-skill middleware hook in ADCPAgentExecutor (closes [#226](https://github.com/adcontextprotocol/adcp-client-python/issues/226)) ([6f9bd26](https://github.com/adcontextprotocol/adcp-client-python/commit/6f9bd26981af36cfa7f17731c683cf521a65732a))
* **server:** pluggable PushNotificationConfigStore on A2A ([#225](https://github.com/adcontextprotocol/adcp-client-python/issues/225)) ([ea21864](https://github.com/adcontextprotocol/adcp-client-python/commit/ea21864fc8d490dc28b9d5a433d6baabc1518f4b))
* **server:** pluggable PushNotificationConfigStore on A2A (closes [#225](https://github.com/adcontextprotocol/adcp-client-python/issues/225)) ([4f64c8d](https://github.com/adcontextprotocol/adcp-client-python/commit/4f64c8d0a87aee8875af1ffe27211bf766778ead))
* **server:** pluggable TaskStore on A2A — unblock production A2A adoption ([69ebccd](https://github.com/adcontextprotocol/adcp-client-python/commit/69ebccd81eccf2970321c379c244cbc0e27873b2))
* **server:** propagate caller_identity from transport into ToolContext ([b4a4c93](https://github.com/adcontextprotocol/adcp-client-python/commit/b4a4c9336f7207c2765c9cfd771e4c124fe3e566))
* **server:** propagate caller_identity from transport into ToolContext ([1f3cf8f](https://github.com/adcontextprotocol/adcp-client-python/commit/1f3cf8f44ce92ede4cb48b125e057494cefe5e89))
* **server:** request-body size cap middleware (closes [#239](https://github.com/adcontextprotocol/adcp-client-python/issues/239)) ([21249a5](https://github.com/adcontextprotocol/adcp-client-python/commit/21249a5032c8afa4f59033dec0b896e6cc4b637d))
* **server:** request-body size cap middleware (closes [#239](https://github.com/adcontextprotocol/adcp-client-python/issues/239)) ([9ccf960](https://github.com/adcontextprotocol/adcp-client-python/commit/9ccf9601182e248861bf0683fbfe55408f76e93f))
* **server:** thread ToolContext through TestControllerStore (closes [#227](https://github.com/adcontextprotocol/adcp-client-python/issues/227)) ([a4c6489](https://github.com/adcontextprotocol/adcp-client-python/commit/a4c6489285ba76b1e57caf09bf561c3aa1461ecf))
* **server:** thread ToolContext through TestControllerStore (closes [#227](https://github.com/adcontextprotocol/adcp-client-python/issues/227)) ([a0d89e3](https://github.com/adcontextprotocol/adcp-client-python/commit/a0d89e3be1a60fa29aa7b1dfd81d4f8b79f9132d))
* **server:** typed handler params via Pydantic annotation (closes [#214](https://github.com/adcontextprotocol/adcp-client-python/issues/214)) ([dcc2c87](https://github.com/adcontextprotocol/adcp-client-python/commit/dcc2c876215546fa5e15d8db4ff7e6ba32c5b442))
* **server:** typed handler params via Pydantic annotation (closes [#214](https://github.com/adcontextprotocol/adcp-client-python/issues/214)) ([4e86e1a](https://github.com/adcontextprotocol/adcp-client-python/commit/4e86e1a55cc09793d18e312f97124622b761feff))
* **server:** TypeVar-bound ADCPHandler for typed ToolContext subclasses ([#223](https://github.com/adcontextprotocol/adcp-client-python/issues/223)) ([d737527](https://github.com/adcontextprotocol/adcp-client-python/commit/d737527027fcf5b77ba40c4502251449633f059d))
* **server:** TypeVar-bound ADCPHandler for typed ToolContext subclasses (closes [#223](https://github.com/adcontextprotocol/adcp-client-python/issues/223)) ([e1eb923](https://github.com/adcontextprotocol/adcp-client-python/commit/e1eb92350e9872cfadbff4bff6a274a49af59c83))
* **server:** wire idempotency errors to wire + capability helper + DX polish ([5b8f353](https://github.com/adcontextprotocol/adcp-client-python/commit/5b8f3531905af238d47f4c0503cce42d6852700e))
* **signing:** async revocation-list + JWKS fetchers ([55a8fd4](https://github.com/adcontextprotocol/adcp-client-python/commit/55a8fd45c7b4b9520c105a7e1e555bc247146e6a))
* **signing:** async revocation-list + JWKS fetchers ([1c2e676](https://github.com/adcontextprotocol/adcp-client-python/commit/1c2e67624f313416b85d1f10b659eb0e84021f3d))
* **signing:** auto-sign outgoing requests via ADCPClient signing kwarg ([551aaf8](https://github.com/adcontextprotocol/adcp-client-python/commit/551aaf8fe97c8b10d8dfef540925d1528c80ec00))
* **signing:** auto-sign outgoing requests via ADCPClient signing kwarg ([c07c946](https://github.com/adcontextprotocol/adcp-client-python/commit/c07c946ff50cdd3ef32db263877ed6aada40e964))
* **signing:** full-wire e2e + DX fixes from integrator walkthrough ([a44896b](https://github.com/adcontextprotocol/adcp-client-python/commit/a44896b743ac4d0694be9b99dce2df103c2cfb0a))
* **signing:** generate_signing_keypair() programmatic API (closes [#217](https://github.com/adcontextprotocol/adcp-client-python/issues/217)) ([dd2b961](https://github.com/adcontextprotocol/adcp-client-python/commit/dd2b9614957d65787868b5effddb2775b74afae9))
* **signing:** generate_signing_keypair() programmatic API (closes [#217](https://github.com/adcontextprotocol/adcp-client-python/issues/217)) ([c6d5505](https://github.com/adcontextprotocol/adcp-client-python/commit/c6d5505c8d6025961f0bd9d6af1041102b4bee82))
* **signing:** IP-pinned httpx transport closes DNS-rebinding TOCTOU ([9a7ee9c](https://github.com/adcontextprotocol/adcp-client-python/commit/9a7ee9c3185e6e7945f06bd27872363f24e164fd))
* **signing:** IP-pinned httpx transport closes DNS-rebinding TOCTOU ([1fa0820](https://github.com/adcontextprotocol/adcp-client-python/commit/1fa082003acd317b9743099fb83dc898614db573))
* **signing:** keygen --encrypt flag and adcp-keygen entry point ([01c0c5d](https://github.com/adcontextprotocol/adcp-client-python/commit/01c0c5d49ffff416144708213f953a6b0cedd5ca))
* **signing:** live revocation-list fetcher with signed JWS verification ([249b79d](https://github.com/adcontextprotocol/adcp-client-python/commit/249b79d956f7962637314042be1116c8f5076cbb))
* **signing:** live revocation-list fetcher with signed JWS verification ([fbe3e90](https://github.com/adcontextprotocol/adcp-client-python/commit/fbe3e909a04b04068acc644413ad78155aa0a6df))
* **signing:** module rename + keygen --encrypt + adcp-keygen entry point ([7bce09a](https://github.com/adcontextprotocol/adcp-client-python/commit/7bce09aa2841a15a7b4461fd707b59bd78156dbd))
* **signing:** PostgreSQL-backed PgReplayStore for multi-instance verifiers ([8118033](https://github.com/adcontextprotocol/adcp-client-python/commit/811803396e13b185f6626c7011b449823b6a1e20))
* **signing:** PostgreSQL-backed PgReplayStore for multi-instance verifiers ([3ad3951](https://github.com/adcontextprotocol/adcp-client-python/commit/3ad395189c44807d48bb6381ef16e591ca1ca5fe))
* strip bogus Literal['reuse'] subclasses from datamodel-codegen ([3684227](https://github.com/adcontextprotocol/adcp-client-python/commit/36842278e3a0085372f14496f4737fba632cc408))
* switch ADCP_VERSION to latest, remap aliases for schema redesign ([253be88](https://github.com/adcontextprotocol/adcp-client-python/commit/253be88ba17cb16723fcae014e9ded288ffe848c))
* sync rc.4 schemas + wire update_rights task ([67119bf](https://github.com/adcontextprotocol/adcp-client-python/commit/67119bf303553ebf2251931ccce4bac5f9c3de50))
* sync rc.4 schemas + wire update_rights task ([6c0e0e9](https://github.com/adcontextprotocol/adcp-client-python/commit/6c0e0e9e3037bd6a59b9f184d9a8fa347cc4fe20))
* sync schemas from protocol bundle with Sigstore verification ([66b9fb1](https://github.com/adcontextprotocol/adcp-client-python/commit/66b9fb1efb7c64d1d7a3e188433fdcef755244d0))
* ToolAnnotations and agent-facing descriptions for all 56 MCP tools ([66729ee](https://github.com/adcontextprotocol/adcp-client-python/commit/66729ee02982b21425a7731de0d17f9c724846d5))
* **types:** AssetsNN semantic aliases + format_category shim + downstream smoke ([f5c6b18](https://github.com/adcontextprotocol/adcp-client-python/commit/f5c6b18cef8b8df5e9e7e77d4a6eb8b4945ce3b4))
* **types:** regenerate schemas + inject Literal-discriminator defaults ([27468aa](https://github.com/adcontextprotocol/adcp-client-python/commit/27468aa358295bdfdeb10af722e422aa6eef9d6b))
* **types:** regenerate schemas + inject Literal-discriminator defaults ([a4bd6d8](https://github.com/adcontextprotocol/adcp-client-python/commit/a4bd6d85bb7ab8799096d58c2f37414f7f7c995f))
* **types:** rename &lt;Type&gt;Asset content types to &lt;Type&gt;Content ([7d0679f](https://github.com/adcontextprotocol/adcp-client-python/commit/7d0679f0884e893f1b691ee41fe8d19edfcd9f0e))
* **webhooks:** adcp.webhooks.deliver() + A2A artifacts conformance ([493f219](https://github.com/adcontextprotocol/adcp-client-python/commit/493f2197f1618b3a626f2f8b99187985dc4a2fab))
* **webhooks:** adcp.webhooks.deliver() one-shot legacy-auth dispatcher ([3bf663d](https://github.com/adcontextprotocol/adcp-client-python/commit/3bf663da780ee4db3d08eaf04ca68ee37dccd281))
* **webhooks:** add from_pem and sign_legacy_webhook helpers ([19a8ab3](https://github.com/adcontextprotocol/adcp-client-python/commit/19a8ab389878013e6901d1ecf15d5d931842131b))
* **webhooks:** RFC 9421 signing + idempotency_key + sender/receiver UX ([63732c8](https://github.com/adcontextprotocol/adcp-client-python/commit/63732c89a3c4955cf79963a139e7627061a4a6f3))
* **webhooks:** RFC 9421 signing + required idempotency_key + sender/receiver UX ([c640b8c](https://github.com/adcontextprotocol/adcp-client-python/commit/c640b8c2d0908f2ba21665e1a8acb4125a50cc6e))


### Bug Fixes

* CI failures - noqa F401 on guard re-exports, dynamic model_rebuild ([378ac50](https://github.com/adcontextprotocol/adcp-client-python/commit/378ac504db796eee0ef9c32bc72f4453324e9c81))
* CI failures and review findings ([9089711](https://github.com/adcontextprotocol/adcp-client-python/commit/90897117e8d59a9881d88dc249418228f589ac8d))
* CI lint - sort imports in types/__init__.py, pin ADCP_VERSION ([cfbdca1](https://github.com/adcontextprotocol/adcp-client-python/commit/cfbdca1ace226782c12e4ec83de36d66da2bad2c))
* CI lint (noqa F401 re-exports) and pin ADCP_VERSION to 3.0.0-rc.3 ([5959e7b](https://github.com/adcontextprotocol/adcp-client-python/commit/5959e7b76f36d6ae584df842b4a84ce164cf4a43))
* **example:** derive port and AGENT_URL from ADCP_PORT env ([1df2306](https://github.com/adcontextprotocol/adcp-client-python/commit/1df230638359ba2cbc1b7d26f1ef5af769e1f811))
* **harness,server:** surface partial/failing storyboards, reuse port on rerun, default list_creatives timestamps ([3b27001](https://github.com/adcontextprotocol/adcp-client-python/commit/3b2700147058fe2b770e387046d3972f64d34147))
* **idempotency:** honor new required Idempotency.supported field in strict gate ([9ff08c9](https://github.com/adcontextprotocol/adcp-client-python/commit/9ff08c9b1a3578e4ff9d240b8df9f1736e5c989c))
* make ergonomic codegen resilient to datamodel-codegen variant suffix shifts ([138179b](https://github.com/adcontextprotocol/adcp-client-python/commit/138179b2b666ef631c4a7ce0e0cab59d09f989a6))
* **migrate+types:** PR [#247](https://github.com/adcontextprotocol/adcp-client-python/issues/247) expert-review followups ([ac505c0](https://github.com/adcontextprotocol/adcp-client-python/commit/ac505c028741ce61efac741a34afdfecddc43eb2))
* mypy override errors in subclass handlers ([d35bc63](https://github.com/adcontextprotocol/adcp-client-python/commit/d35bc6310119c88a9205eae9e7961be674e030d7))
* mypy valid-type in generated code, subclass handler param types ([eb4e027](https://github.com/adcontextprotocol/adcp-client-python/commit/eb4e027664529e9236ec6db2f0a13ae396359bda))
* noqa F401 on new re-exports (TRANSIENT_CODES, MEDIA_BUY_STATE_MACHINE, PaginationResponse) ([b4059c5](https://github.com/adcontextprotocol/adcp-client-python/commit/b4059c5763d1972635f36dc1f74345dbcb546196))
* resolve CI failures from latest-tracking regeneration ([8b49e1a](https://github.com/adcontextprotocol/adcp-client-python/commit/8b49e1a47335222688ee811abe8173c2a97c9db3))
* **sdk:** remove stray noqa, correct capabilities_response docstring ([05ac689](https://github.com/adcontextprotocol/adcp-client-python/commit/05ac68914d608fbbcffa3f3576355eebfc7bc9fc))
* **sdk:** SDK cheap fixes for DX Stream B1 ([e67e8ed](https://github.com/adcontextprotocol/adcp-client-python/commit/e67e8ed9643c117b8545143b26b5904da56285b1))
* **sdk:** three cleanups from round-4 validator findings ([d7e93f7](https://github.com/adcontextprotocol/adcp-client-python/commit/d7e93f73171f3c4f9d153e1cc207747120ed5ee9))
* **seller:** declare compliance_testing block and add missing force_creative_status scenario ([5de1c61](https://github.com/adcontextprotocol/adcp-client-python/commit/5de1c61062ee628e9dcfe786c948db58e5727fb8))
* **server:** expert-review followups — tenant-scoped idempotency, A2A context_factory, ContextVar safety ([5f053af](https://github.com/adcontextprotocol/adcp-client-python/commit/5f053af8aa4eca0369a3355d6c7dd498a155713f))
* **server:** PR [#230](https://github.com/adcontextprotocol/adcp-client-python/issues/230) expert-review followups — cross-tenant isolation + hardening ([bb2bc67](https://github.com/adcontextprotocol/adcp-client-python/commit/bb2bc67abb5d470fa126a27c232fea2343f9495e))
* **server:** PR [#232](https://github.com/adcontextprotocol/adcp-client-python/issues/232) expert-review followups — scope_provider injection, loud anonymous fallback, SSRF/secret-storage docs ([5d3eff8](https://github.com/adcontextprotocol/adcp-client-python/commit/5d3eff8140ab7825947b72c4f04bda9ffc9ae8f0))
* **server:** PR [#233](https://github.com/adcontextprotocol/adcp-client-python/issues/233) expert-review followups — security docs + retry/transform tests ([afbbfac](https://github.com/adcontextprotocol/adcp-client-python/commit/afbbfac5c87581cd0096d525c1dbf264356020ea))
* **server:** PR [#234](https://github.com/adcontextprotocol/adcp-client-python/issues/234) expert-review followups — TypeVar tests & docs ([df5898c](https://github.com/adcontextprotocol/adcp-client-python/commit/df5898ca7dead3acb25fc7d5571ae1f88c55a52e))
* **server:** PR [#237](https://github.com/adcontextprotocol/adcp-client-python/issues/237) expert-review followups ([a415786](https://github.com/adcontextprotocol/adcp-client-python/commit/a415786b6fdf02bf43c19996a41488c881b81f68))
* **server:** PR [#238](https://github.com/adcontextprotocol/adcp-client-python/issues/238) expert-review followups ([a198656](https://github.com/adcontextprotocol/adcp-client-python/commit/a1986562eb0e36bf1ec3a4e2136082c93b994698))
* **server:** PR [#238](https://github.com/adcontextprotocol/adcp-client-python/issues/238) review round 2 — MCP field surfacing + custom-validator doc ([4d4e2e7](https://github.com/adcontextprotocol/adcp-client-python/commit/4d4e2e7784bbb970e5d7cd8505d36f5570c96c4e))
* **server:** PR [#241](https://github.com/adcontextprotocol/adcp-client-python/issues/241) expert-review followups ([ce189f8](https://github.com/adcontextprotocol/adcp-client-python/commit/ce189f84f5392f45964fa3e5e355436e675222e9))
* **server:** PR [#244](https://github.com/adcontextprotocol/adcp-client-python/issues/244) expert-review followups ([6af9a7d](https://github.com/adcontextprotocol/adcp-client-python/commit/6af9a7d00bdcda5378b8225de2531f99cea1d1a2))
* **server:** PR [#245](https://github.com/adcontextprotocol/adcp-client-python/issues/245) code-review followups — fail loudly on silent-skip paths ([a0a3077](https://github.com/adcontextprotocol/adcp-client-python/commit/a0a3077b9d99f60f2aed2300de139aebd7fc0e41))
* **server:** Threat 3 merge-blocker on advertised-tools gate ([11abb3b](https://github.com/adcontextprotocol/adcp-client-python/commit/11abb3b7cced61611d856763e42995a9c32966b1))
* **signing:** PR [#206](https://github.com/adcontextprotocol/adcp-client-python/issues/206) reviewer fixes — IDN normalization, HTTPS_PROXY, fail-closed reuse ([b432b03](https://github.com/adcontextprotocol/adcp-client-python/commit/b432b03003c5cf2d1afa393d7dd5d714e0785405))
* **signing:** PR [#243](https://github.com/adcontextprotocol/adcp-client-python/issues/243) expert-review followups ([e616307](https://github.com/adcontextprotocol/adcp-client-python/commit/e6163077b84a73523b89c653154694d250124ea2))
* **signing:** round-2 review — harden identifier check, drop invalid SQL ([4ea1d8f](https://github.com/adcontextprotocol/adcp-client-python/commit/4ea1d8fa0cd3e3f44130dcbc02ec9c0079116978))
* **signing:** round-2 review — harden issuer/Last-Modified, slide next_update on 304 ([8dbcbc5](https://github.com/adcontextprotocol/adcp-client-python/commit/8dbcbc521ce4d3e3a064583b130488ade388d317))
* sort TYPE_CHECKING imports in base.py and mcp_tools.py (I001) ([71f7448](https://github.com/adcontextprotocol/adcp-client-python/commit/71f7448a2f91203dcfc3516ae0f74dda6739910f))
* suppress E501 in mcp_tools.py (tool descriptions are long strings) ([9eea1d9](https://github.com/adcontextprotocol/adcp-client-python/commit/9eea1d9f79df6f4752337eaa9e7a6c914e9e270f))
* **webhooks,seller,signals:** round-6 P1+P2 DX fixes ([6e51519](https://github.com/adcontextprotocol/adcp-client-python/commit/6e515192b9eac96f90cd1e0923425b3121201f62))
* **webhooks:** align deliver() with canonical compact-separator on-wire form ([92240d5](https://github.com/adcontextprotocol/adcp-client-python/commit/92240d5c481f743569447c1c63d8ef1637c53436))
* **webhooks:** re-apply compact-separator signer after rebase onto 3.0 GA ([166aa1b](https://github.com/adcontextprotocol/adcp-client-python/commit/166aa1b46e63c11eb5438f41a937e8dcd60114a6))
* **webhooks:** sign compact-separator JSON to match httpx wire format ([1c2a527](https://github.com/adcontextprotocol/adcp-client-python/commit/1c2a52798bf8415fac6b9d920d3d984410e02114))
* **webhooks:** verifier fails closed when raw_body missing (adcp[#2478](https://github.com/adcontextprotocol/adcp-client-python/issues/2478)) ([9a906a9](https://github.com/adcontextprotocol/adcp-client-python/commit/9a906a9466cda28f4043f1fa05895401385fccba))


### Documentation

* **creative:** fix NameError in build_creative fallback ([afe6afb](https://github.com/adcontextprotocol/adcp-client-python/commit/afe6afbae671594e185c236dda6f0aa25123f8df))
* **creative:** remove anti-patterns from build-creative-agent skill ([9e42039](https://github.com/adcontextprotocol/adcp-client-python/commit/9e42039cc7da9191dee1efe0737fb2f642337ba0))
* **design:** FastMCP native registration investigation ([#209](https://github.com/adcontextprotocol/adcp-client-python/issues/209)) ([d32694f](https://github.com/adcontextprotocol/adcp-client-python/commit/d32694f00fa7aef27d54b0671233646c6fdf5bbf))
* **design:** FastMCP native registration investigation (closes [#209](https://github.com/adcontextprotocol/adcp-client-python/issues/209)) ([ba3cb73](https://github.com/adcontextprotocol/adcp-client-python/commit/ba3cb733ddd4e4d60aad8c5a2296acd02d98b9ad))
* fix DX gaps in skill docs from real-world agent builds ([dc63f8b](https://github.com/adcontextprotocol/adcp-client-python/commit/dc63f8bdb7f4a1fb9eb3eac3e529219d3d26fe29))
* **generative-seller:** fix invalid enums and document generative tools ([99e9f2e](https://github.com/adcontextprotocol/adcp-client-python/commit/99e9f2e2e366c48b578cc3f7dd38e66093c09357))
* link upstream codegen bug in post_generate_fixes ([ee6f52c](https://github.com/adcontextprotocol/adcp-client-python/commit/ee6f52c745ae44e692cc544c25a21779558ace3f))
* **retail:** fix invalid capability enum and storyboard invocation ([249acb1](https://github.com/adcontextprotocol/adcp-client-python/commit/249acb1b5c2a4f926e786bf4a5dda70b8b9dd2e8))
* **seller,retail:** teach proposal refine schema and webhook emission ([9723eef](https://github.com/adcontextprotocol/adcp-client-python/commit/9723eefe687571a5de1bd2da2472b9c293edf612))
* **seller:** correct invalid schema claims and serve() signature ([f3a77c6](https://github.com/adcontextprotocol/adcp-client-python/commit/f3a77c65d0b7efa2ae36c3a9f561be0d074e62fe))
* **seller:** replace false compliance_testing auto-wire with explicit kwarg ([8d74805](https://github.com/adcontextprotocol/adcp-client-python/commit/8d74805c1acf9868a13d1edfce03ab46970dcde7))
* **signals,generative:** correct idempotency.wrap decorator usage ([3ba41e6](https://github.com/adcontextprotocol/adcp-client-python/commit/3ba41e61d7f5c18b5ac5a489d8c29e4897eabe0d))
* **signals,seller:** round-2 validator findings ([2b941d0](https://github.com/adcontextprotocol/adcp-client-python/commit/2b941d0c49dbfb4ccd85f9e93836b1832ceb14c2))
* **signals:** tighten signal_ids filter, idempotency, validation command ([e0fb6d6](https://github.com/adcontextprotocol/adcp-client-python/commit/e0fb6d650adf4db90ad3d86716b08606b59c6103))
* **skills,examples:** align webhook + proposal examples with real SDK and schema ([a9e2a63](https://github.com/adcontextprotocol/adcp-client-python/commit/a9e2a63f8d8df6bd61a0e35124097017148e22c6))
* **skills:** round-8 DX polish — scope seller, self-contain signals ([3686e38](https://github.com/adcontextprotocol/adcp-client-python/commit/3686e38e5adbe8921dfca8cc14b8862ada9a1eb5))
* **skills:** round-8 DX polish — scope seller, self-contain signals ([8b5075c](https://github.com/adcontextprotocol/adcp-client-python/commit/8b5075c68d37fe5771ee758752e8db835edff4c8))


### Miscellaneous Chores

* mark 4.0 breaking change for release-please ([8d0c430](https://github.com/adcontextprotocol/adcp-client-python/commit/8d0c4302a4cd35beb070827082305c729bc0dd78))

## [4.0.0b1] — 2026-04-20

First 4.0 beta. See [MIGRATION_v3_to_v4.md](MIGRATION_v3_to_v4.md) for upgrade
instructions and before/after examples.

### Breaking changes

* **Removed types (no replacement stubs):** `BrandManifest`, `FormatCategory`,
  `DeliverTo`, `PromotedProducts`, `PromotedOfferings`, `Pricing`,
  `PackageStatus`. These were removed upstream from the AdCP spec. Inline any
  dict payloads, or use the spec-current replacements (`BrandReference` for
  `BrandManifest`; `MediaBuyStatus` for the former `PackageStatus`).
* **`ResolvedBrand.brand_manifest` field removed.** Use `ResolvedBrand.brand`.
  The cross-populate validator that mirrored `brand` ↔ `brand_manifest` is gone.
* **`CreateMediaBuyRequest.brand_manifest` → `brand`**, matching the spec. The
  request now takes a `BrandReference`, not an inline brand manifest dict.
* **Numbered discriminated-union classes renumbered.** `Assets5…Assets14`
  from 3.x now correspond to higher-numbered variants (`Assets57…Assets149`
  depending on the response). Import via semantic aliases from `adcp.types`
  (e.g., `CreateMediaBuySuccessResponse`) instead of numbered classes. Raw
  `Assets*` imports are unsupported — they are code-generation artifacts and
  will shift again.
* **Asset-content types renamed `<Type>Asset` → `<Type>Content`** (issue
  #221). The payload-describing types (`AudioAsset`, `CssAsset`, `HtmlAsset`,
  `ImageAsset`, `JavascriptAsset`, `TextAsset`, `UrlAsset`, `VideoAsset`,
  `WebhookAsset`) are renamed to `AudioContent`, `CssContent`, `HtmlContent`,
  `ImageContent`, `JavascriptContent`, `TextContent`, `UrlContent`,
  `VideoContent`, `WebhookContent`. This disambiguates payload types from
  the slot-describing `<Type>FormatAsset` family (`VideoContent` describes
  what a creative delivers; `VideoFormatAsset` is a slot inside a format
  definition). The old names are no longer exported from `adcp.types`. See
  `MIGRATION_v3_to_v4.md` for a search-and-replace.

### Non-breaking

* `__version__` is now sourced from installed distribution metadata
  (`importlib.metadata.version("adcp")`) so it always matches `pyproject.toml`.
* Added top-level re-exports: `TargetingOverlay`, `AdvertiserIndustry`,
  `KellerType`, `BrandSource`. Any generated type not on the top-level
  surface is available from `adcp.types` — `adcp.types.generated_poc.*`
  is internal and not supported for direct import.

## [3.12.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.11.0...v3.12.0) (2026-04-16)


### Features

* skill-based agent generation with storyboard validation ([e0eef4c](https://github.com/adcontextprotocol/adcp-client-python/commit/e0eef4c35a7a8bb2149e1cf67d2fc81ec9bbfafe))
* skill-based agent generation with storyboard validation ([d9d8778](https://github.com/adcontextprotocol/adcp-client-python/commit/d9d8778826a5d415798605f3dd94482318b0499c))


### Bug Fixes

* authorization_type takes precedence over stale properties key ([3fc9ed0](https://github.com/adcontextprotocol/adcp-client-python/commit/3fc9ed00b5641d69065c31cde0de9b8b2d9ba3f0))
* resolve property_ids and property_tags in get_all_properties() ([ef55dcb](https://github.com/adcontextprotocol/adcp-client-python/commit/ef55dcba4e177cd09f574bf9387c8585c57ed5ad))
* resolve property_ids and property_tags in get_all_properties() ([eca6bd7](https://github.com/adcontextprotocol/adcp-client-python/commit/eca6bd7383f0d9440233300d8d9415cfba5680f5)), closes [#172](https://github.com/adcontextprotocol/adcp-client-python/issues/172)
* SSRF protections for authoritative_location redirects ([6d1c962](https://github.com/adcontextprotocol/adcp-client-python/commit/6d1c9628d22993cb4f74206edbf084baab3608ac))

## [3.11.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.10.0...v3.11.0) (2026-04-04)


### Features

* ADCP 3.0.0-rc3 spec + full registry support ([c3f6937](https://github.com/adcontextprotocol/adcp-client-python/commit/c3f6937356bb7c37d7de665e2e674fb4a590da25))
* implement ADCP 3.0.0-rc3 spec with full registry support ([edc9578](https://github.com/adcontextprotocol/adcp-client-python/commit/edc95783be1b71c662a4b1df5878ffd1cc51ee15))
* remove all backward-compat type stubs for clean 3.0.0 surface ([a45cdf5](https://github.com/adcontextprotocol/adcp-client-python/commit/a45cdf568d40b10c7c6660f5858b6c712abcf0dd))


### Bug Fixes

* add backward-compat FormatCategory/FormatType enum stub ([756ed50](https://github.com/adcontextprotocol/adcp-client-python/commit/756ed50cf192754c1b03a1a15f1e9757a81f9480))
* clean up formatting in registry_sync and property_registry ([44a1914](https://github.com/adcontextprotocol/adcp-client-python/commit/44a191446bc55a0213873846835d1f5358f58cff))
* remove accidental cursor file, add to .gitignore ([2650f8e](https://github.com/adcontextprotocol/adcp-client-python/commit/2650f8e1138122a39bc6fbb50bd052596ab99e54))
* remove stale buyer_campaign_ref from MCP tool schemas ([3c7a2c8](https://github.com/adcontextprotocol/adcp-client-python/commit/3c7a2c89f3d84523810be2bdde9e8f22f7d75bab))

## [3.10.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.9.1...v3.10.0) (2026-03-17)


### Features

* add feature capability validation API (supports/require) ([d550c08](https://github.com/adcontextprotocol/adcp-client-python/commit/d550c081617fa6535b3ed896279f62e11b589283))
* add RC2 governance support and full spec coverage ([523ea20](https://github.com/adcontextprotocol/adcp-client-python/commit/523ea204d3a137480681cbf13dd0005517e3a3d9))
* feature capability validation API (supports/require) ([f86f6c4](https://github.com/adcontextprotocol/adcp-client-python/commit/f86f6c4b880fe26fa73da026609b9a9f5154c27c))
* filter MCP tools by handler type, add webhook replay protection ([ae7f378](https://github.com/adcontextprotocol/adcp-client-python/commit/ae7f3781069b09e9f583bb010c3848c6ba7b19a2))


### Bug Fixes

* address PR review feedback (FieldModel collision, handler boilerplate, deprecation aliases) ([b9eac61](https://github.com/adcontextprotocol/adcp-client-python/commit/b9eac61a1a76a295c7a2cd31fb92d40487dfee7d))
* replace TypeAdapter with model_validate, tighten slug regex ([103d616](https://github.com/adcontextprotocol/adcp-client-python/commit/103d6168374feeace1630eb442e8df7f5469a426))
* require webhook headers when secret configured, restrict unknown handler tools ([12f1080](https://github.com/adcontextprotocol/adcp-client-python/commit/12f1080106cdd2e962bc7cf544b6c9bb23cb42dd))
* walk MRO for tool filtering, add tool name validation ([39985e5](https://github.com/adcontextprotocol/adcp-client-python/commit/39985e51153c125866ee354ac5862f0eb525362f))

## [3.9.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.9.0...v3.9.1) (2026-03-16)


### Bug Fixes

* flatten validation-only oneOf schemas for consumer subclassing ([3171a95](https://github.com/adcontextprotocol/adcp-client-python/commit/3171a953b41dc770098998179aed9381064bc773))
* flatten validation-only oneOf schemas to single subclassable classes ([c242a7d](https://github.com/adcontextprotocol/adcp-client-python/commit/c242a7d09b652dab8f761136b771b76617078cf3))

## [3.9.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.8.0...v3.9.0) (2026-03-13)


### Features

* add RootModel __getattr__ proxy and minimal sales agent example ([#150](https://github.com/adcontextprotocol/adcp-client-python/issues/150)) ([f1c3990](https://github.com/adcontextprotocol/adcp-client-python/commit/f1c39902eada08c3dc0ff9128649a8fde6d2b49a)), closes [#145](https://github.com/adcontextprotocol/adcp-client-python/issues/145) [#148](https://github.com/adcontextprotocol/adcp-client-python/issues/148)


### Bug Fixes

* add raw_body support for verification and use Unix timestamps ([4a663e5](https://github.com/adcontextprotocol/adcp-client-python/commit/4a663e5406bbf2ce533842fdd74c749a79f802d1)), closes [#151](https://github.com/adcontextprotocol/adcp-client-python/issues/151)
* set AdCPBaseModel extra='ignore' and remove obsolete generate_models.py ([#157](https://github.com/adcontextprotocol/adcp-client-python/issues/157)) ([21077d9](https://github.com/adcontextprotocol/adcp-client-python/commit/21077d91136088ad170a81800dd2446925f20591))
* unwrap RootModel unions for consumer subclassing ([#156](https://github.com/adcontextprotocol/adcp-client-python/issues/156)) ([8fd7b92](https://github.com/adcontextprotocol/adcp-client-python/commit/8fd7b92bbd13c9d5922fd40dea51cc37c155a776))
* use default JSON separators for webhook HMAC signing ([2223f6e](https://github.com/adcontextprotocol/adcp-client-python/commit/2223f6ed5a2d32cdc000a273f591d30e3cfe2e65))
* use default JSON separators for webhook HMAC signing ([0dd495f](https://github.com/adcontextprotocol/adcp-client-python/commit/0dd495fd83c0c0d4df99fe794d878d0f98735cc8)), closes [#151](https://github.com/adcontextprotocol/adcp-client-python/issues/151)

## [3.8.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.7.0...v3.8.0) (2026-03-01)


### Features

* sync schemas to AdCP 3.0.0-rc.1 ([#141](https://github.com/adcontextprotocol/adcp-client-python/issues/141)) ([b0497a3](https://github.com/adcontextprotocol/adcp-client-python/commit/b0497a3f673ce3b11b4f6c29d8b49953c1d5afd0))


### Bug Fixes

* resolve CI failures from GetSignalsRequest union alias ([#144](https://github.com/adcontextprotocol/adcp-client-python/issues/144)) ([f135651](https://github.com/adcontextprotocol/adcp-client-python/commit/f13565106931f471b0304e58b4c182679777676b))

## [3.7.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.6.0...v3.7.0) (2026-02-28)


### Features

* sync upstream schemas from PR 1252 ([#139](https://github.com/adcontextprotocol/adcp-client-python/issues/139)) ([6cb2b6c](https://github.com/adcontextprotocol/adcp-client-python/commit/6cb2b6cb994f5c0ae2f440768f9b1b90009c4337))

## [3.6.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.5.0...v3.6.0) (2026-02-23)


### Features

* sync upstream schemas and add get_media_buys ([#136](https://github.com/adcontextprotocol/adcp-client-python/issues/136)) ([3625a2b](https://github.com/adcontextprotocol/adcp-client-python/commit/3625a2ba085bf35b26f899915d104d9f53508724))

## [3.5.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.4.0...v3.5.0) (2026-02-22)


### Features

* sync upstream catalog schemas and add catalog type support ([#134](https://github.com/adcontextprotocol/adcp-client-python/issues/134)) ([9de1eda](https://github.com/adcontextprotocol/adcp-client-python/commit/9de1eda9bb056dc16122000c2affedab63e323e9))

## [3.4.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.3.0...v3.4.0) (2026-02-20)


### Features

* add brand and property registry lookup methods ([#130](https://github.com/adcontextprotocol/adcp-client-python/issues/130)) ([9bfb0fb](https://github.com/adcontextprotocol/adcp-client-python/commit/9bfb0fb2f02890883ccdb0e052a6bf3c4ae86317)), closes [#129](https://github.com/adcontextprotocol/adcp-client-python/issues/129)
* export PromotedOfferingsRequirement and PromotedOfferingsAssetRequirements, sync latest schemas ([#133](https://github.com/adcontextprotocol/adcp-client-python/issues/133)) ([9216ea7](https://github.com/adcontextprotocol/adcp-client-python/commit/9216ea799f21f508feb86484dcac7c05d732495d))
* sync ADCP schemas to latest, add brand compat and AAO member API ([#132](https://github.com/adcontextprotocol/adcp-client-python/issues/132)) ([0dca021](https://github.com/adcontextprotocol/adcp-client-python/commit/0dca021f5c9a1b8e46326cb9cfcea314bfb28710))

## [3.3.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v3.2.0...v3.3.0) (2026-02-12)


### Features

* sync ADCP schema to 3.0.0-beta.3 with new operations and forecasting ([#127](https://github.com/adcontextprotocol/adcp-client-python/issues/127)) ([bb611c3](https://github.com/adcontextprotocol/adcp-client-python/commit/bb611c3ad011990bc85f7562cf764ba7e5eb06c1))

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
