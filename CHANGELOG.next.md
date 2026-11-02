# Changelog

## [10.0.0-beta.6](https://github.com/adcontextprotocol/adcp-client-python/compare/v10.0.0-beta.5...v10.0.0-beta.6) (2026-11-02)


### ⚠ BREAKING CHANGES

* **accounts:** a list_account_changes handler returning an adcp or seller_operator record without changed_paths now fails the call, and idempotency_key is stripped from every record not attributed to the caller via adcp.account_change_feed.attribute_account_change.
* ReconcileReport.accounts is a tuple of ReconcileAccount instead of account-id strings, and to_dict serves objects under accounts. Read report.account_ids for the ids.
* **media-buy:** WebhookSender.send_media_buy_delivery, send_prepared for a prepared media_buy_delivery, and the artifact helpers given registration= raise WebhookSigningProfileUnavailableError instead of sending when a registration without authentication would go out through a bearer or HMAC sender or names web-bot-auth. Use WebhookSender.from_jwk or from_pem for these registrations. WebhookReceiver no longer passes aggregated_totals through on media_buy_delivery results.
* **codegen:** the _1 alias members of generated enums (System.zip_1, Field17.ad_request_url_1, ...) are removed. Use the real member (System.zip); System("zip") and member.value are unchanged.
* a server asked for a task its caller's release does not define now fails with UNSUPPORTED_FEATURE instead of VERSION_UNSUPPORTED. validate_platform fails when a platform declares list_proposals or media_buy.collection_catalog without the method. Agent-level notification registrations of proposal.created, proposal.offered, collection_card.*, collection_catalog.bulk_change or creative.staged_update_changed require all_authorized_accounts true.
* **adagents:** resolve_properties_for_agent(mode="permissive") raises ValueError unless publisher_domain= (or allow_any_domain=True) is passed, and with publisher_domain its bare-entry fallback returns only domain properties under that domain. See MIGRATION_v9_to_v10.md.

### Features

* **accounts:** add accounts.change and use for account requests ([6207e4c](https://github.com/adcontextprotocol/adcp-client-python/commit/6207e4c75240fc634d7f910d729b4637d12fbf74))
* **accounts:** add accounts.change and use for account requests ([40504d9](https://github.com/adcontextprotocol/adcp-client-python/commit/40504d9deef521bad05af2f89d6e709ae5c2efc7))
* **accounts:** enforce change feed changed_paths and per-reader idempotency_key ([#1647](https://github.com/adcontextprotocol/adcp-client-python/issues/1647)) ([#1805](https://github.com/adcontextprotocol/adcp-client-python/issues/1805)) ([d9ba90c](https://github.com/adcontextprotocol/adcp-client-python/commit/d9ba90cb430ca8bc18f87d7a768dc71fdb5801b6))
* **accounts:** read a pending account change back with change_status ([539d829](https://github.com/adcontextprotocol/adcp-client-python/commit/539d829b2d01c10216b4e7fddd5262aa39f4a938))
* **adagents:** attribute properties to a publisher domain, gate permissive resolution, verify cross-domain claims ([#1711](https://github.com/adcontextprotocol/adcp-client-python/issues/1711)) ([f775e13](https://github.com/adcontextprotocol/adcp-client-python/commit/f775e1344c94def42c5d018ecc9f45185a7cf2be))
* **canonical-formats:** rewrite 3.3 playback pixel trackers to custom for 3.2 peers ([#1643](https://github.com/adcontextprotocol/adcp-client-python/issues/1643)) ([#1800](https://github.com/adcontextprotocol/adcp-client-python/issues/1800)) ([41c3988](https://github.com/adcontextprotocol/adcp-client-python/commit/41c39887dbbfbd255a1e099b13feb97df635505e))
* **decisioning:** let a raised AdcpError reject a request with every error ([#1780](https://github.com/adcontextprotocol/adcp-client-python/issues/1780)) ([#1804](https://github.com/adcontextprotocol/adcp-client-python/issues/1804)) ([0439d67](https://github.com/adcontextprotocol/adcp-client-python/commit/0439d676584391cb310fe84463bdfca51e8575e9))
* **media-buy:** enforce AdCP 3.3 provisional-buy, revision-origin and webhook-signing rules ([#1807](https://github.com/adcontextprotocol/adcp-client-python/issues/1807)) ([c6aa913](https://github.com/adcontextprotocol/adcp-client-python/commit/c6aa91332979a3f798eecafcadf480817ad9d520)), closes [#1644](https://github.com/adcontextprotocol/adcp-client-python/issues/1644)
* per-account reconcile report and fleet reconcile for reporting subscriptions ([#1802](https://github.com/adcontextprotocol/adcp-client-python/issues/1802)) ([33a8bb0](https://github.com/adcontextprotocol/adcp-client-python/commit/33a8bb0e645eb9734e17b5e8d65f2ef2bd011c9f))
* **reporting:** add buyer reconcile and explain on client.reporting ([c830344](https://github.com/adcontextprotocol/adcp-client-python/commit/c830344ed5297551f9bf9ef0beeea756027f12ab))
* **reporting:** add buyer reconcile and explain on client.reporting ([dbf329f](https://github.com/adcontextprotocol/adcp-client-python/commit/dbf329f11a5d603ca795d075bfed3ce23bf9312e))
* **reporting:** add the buyer receipts policy to reconcile ([9072f35](https://github.com/adcontextprotocol/adcp-client-python/commit/9072f3511113695e26315d55da2d57bc7171cef1))
* **reporting:** add the buyer receipts policy to reconcile ([42b919e](https://github.com/adcontextprotocol/adcp-client-python/commit/42b919e4fab1983d1dfb8c8363b083948105fddd))
* **reporting:** add the reporting test kit, reference seller and pytest fixture ([0dac622](https://github.com/adcontextprotocol/adcp-client-python/commit/0dac622ff94ae020154af66ab03886c99cf3a8e9))
* **reporting:** add the reporting test kit, reference seller and pytest fixture ([1842153](https://github.com/adcontextprotocol/adcp-client-python/commit/184215360b7b1c1321289c79b4da22555cb9866d))
* **reporting:** derive buyer reporting events with poll, events and handle_webhook ([49ea7de](https://github.com/adcontextprotocol/adcp-client-python/commit/49ea7deab231f23533548b77ce8e8f4642fc07fe))
* **reporting:** derive buyer reporting events with poll, events and handle_webhook ([f0e8c9d](https://github.com/adcontextprotocol/adcp-client-python/commit/f0e8c9d1e072e7ca440c221717913daffabd9fc4))
* **reporting:** explain coverage with a shared CoverageTrace ([a025dbd](https://github.com/adcontextprotocol/adcp-client-python/commit/a025dbd77bf9d560ffc9997a14a10ef945bdead8))
* **reporting:** explain coverage with a shared CoverageTrace ([6657c75](https://github.com/adcontextprotocol/adcp-client-python/commit/6657c75a18c86420192b3ec0cc4aa97e7c697293))
* **reporting:** serve the reference seller standalone and pass the TypeScript storyboard ([76812b8](https://github.com/adcontextprotocol/adcp-client-python/commit/76812b8bdb836b8d061410bd9fe124fb82959865))
* **reporting:** serve the reference seller standalone and pass the TypeScript storyboard ([fa7fccf](https://github.com/adcontextprotocol/adcp-client-python/commit/fa7fccfb0020958f64f1cce18c897298fe44c068))
* serve request_account and account requests from the AccountStore ([#1808](https://github.com/adcontextprotocol/adcp-client-python/issues/1808)) ([e556c3b](https://github.com/adcontextprotocol/adcp-client-python/commit/e556c3b1ed07e1669996e79a6b481ccf3c05ef28))
* **types:** name macro declaration variants by their asset arm ([#1801](https://github.com/adcontextprotocol/adcp-client-python/issues/1801)) ([64bc9b7](https://github.com/adcontextprotocol/adcp-client-python/commit/64bc9b773d77aba5c31e0b911d45f58306518132)), closes [#1783](https://github.com/adcontextprotocol/adcp-client-python/issues/1783)
* wire the list_proposals and list_collections tasks ([#1803](https://github.com/adcontextprotocol/adcp-client-python/issues/1803)) ([0dc1b21](https://github.com/adcontextprotocol/adcp-client-python/commit/0dc1b21e23ee8136643cf37ffd0c2aafc9254b48))


### Bug Fixes

* **accounts:** keep the account status when use re-points a key ([80ce9f0](https://github.com/adcontextprotocol/adcp-client-python/commit/80ce9f02c4dbc9eeee3057af6233299787395dd8))
* **codegen:** name each generated enum value once ([#1799](https://github.com/adcontextprotocol/adcp-client-python/issues/1799)) ([0357253](https://github.com/adcontextprotocol/adcp-client-python/commit/03572535a5651f5ffda217bd289ec303492f6eb5))
* reporting re-grant chain, adjustment offers and rejected receipts match TS ([#1806](https://github.com/adcontextprotocol/adcp-client-python/issues/1806)) ([7d36906](https://github.com/adcontextprotocol/adcp-client-python/commit/7d369066bdf76a62b2706b11ba65bc02df47af4e))
* **reporting:** compare set-valued subscription arrays without order on readback ([99abea8](https://github.com/adcontextprotocol/adcp-client-python/commit/99abea8c95851e69c5a57a7ae9e2ba8186fd4303))
* **reporting:** compare set-valued subscription arrays without order on readback ([0697a2d](https://github.com/adcontextprotocol/adcp-client-python/commit/0697a2d6148b25e69046d6eb45328e1ac8fd3f4a))
* **reporting:** consume the events doorbell before each poll ([25a6e7b](https://github.com/adcontextprotocol/adcp-client-python/commit/25a6e7b0e201b8273831c3dc31944545bf35c389))
* **reporting:** explain periods before an open revocation as owed:withheld ([9eb96a0](https://github.com/adcontextprotocol/adcp-client-python/commit/9eb96a012f48454c3c825e83919bcf5047dd1fde))
* **reporting:** fold whether the period has ended into the trace digest ([f1c23ca](https://github.com/adcontextprotocol/adcp-client-python/commit/f1c23ca5e9237c34a12917c4296aed9ac6eb1c23))
* **reporting:** give each account rejoin a new account.covered event id ([db016f8](https://github.com/adcontextprotocol/adcp-client-python/commit/db016f898fbd49d9e33b2e8b3fc67335c6c726ad))
* **reporting:** isolate a failing receipt commit and state its at-least-once contract ([80cfb79](https://github.com/adcontextprotocol/adcp-client-python/commit/80cfb798439c0df3a6b13d53116bef2f9ae77882))
* **reporting:** keep subscriptions and losses across a partial events poll ([4c64141](https://github.com/adcontextprotocol/adcp-client-python/commit/4c641410944bfd1d02fe82bc0e40ab5d82c06894))
* **reporting:** keep the events loop running after an ack conflict ([cf71c7d](https://github.com/adcontextprotocol/adcp-client-python/commit/cf71c7dfad87e367ec36db0f83f7710ef1286389))
* **reporting:** key status checkpoints by resolved offering ([d490481](https://github.com/adcontextprotocol/adcp-client-python/commit/d49048161de72fe3499da1938ca406a304e278e1))
* **reporting:** key status checkpoints by resolved offering ([9494c95](https://github.com/adcontextprotocol/adcp-client-python/commit/9494c95b56bf93b21a28fbf2b95b97b0d1b4d728))
* **reporting:** list each flight period once in the not-owed audit ([9defc4c](https://github.com/adcontextprotocol/adcp-client-python/commit/9defc4c13f4a32d670d11174c5e2a92cdbf40634))
* **reporting:** make the managed seller pass the SDK's own capability validation ([922fb87](https://github.com/adcontextprotocol/adcp-client-python/commit/922fb8777f8f158f285fa5bd2703b581f25eddec))
* **reporting:** make the managed seller pass the SDK's own capability validation ([c6a2575](https://github.com/adcontextprotocol/adcp-client-python/commit/c6a2575075c07601b22e5c11a9547e7b68f7cc75))
* **reporting:** name reporting_revision_id on the not-found answer for an unknown revision ([#1796](https://github.com/adcontextprotocol/adcp-client-python/issues/1796)) ([8cc8818](https://github.com/adcontextprotocol/adcp-client-python/commit/8cc8818a9ecc48e4b364320df0c53daedfd59ece)), closes [#1669](https://github.com/adcontextprotocol/adcp-client-python/issues/1669)
* **reporting:** read each package offering snapshot from get_media_buys ([131b9ba](https://github.com/adcontextprotocol/adcp-client-python/commit/131b9ba9536755445f13c045500d54f8f5d1007a))
* **reporting:** read each package offering snapshot from get_media_buys ([f1479f5](https://github.com/adcontextprotocol/adcp-client-python/commit/f1479f5d79e7c5813c7dc207804ef3878b999e5c))
* **reporting:** receipt each materialization once and isolate verifier failures ([b1cc1c4](https://github.com/adcontextprotocol/adcp-client-python/commit/b1cc1c4ba6e2a96e85f937adf872f17118e772a0))
* **reporting:** record the bare inspector code for an unverifiable revision ([cc9b9e0](https://github.com/adcontextprotocol/adcp-client-python/commit/cc9b9e08a577ea52eb2da7e390344088c3c239bd))
* **reporting:** refuse coverage input only where a schedule reads it ([fc370c6](https://github.com/adcontextprotocol/adcp-client-python/commit/fc370c61a7e55f082ac67011fbb2d571828e110a))
* **reporting:** report a failed principal read as a reconcile issue ([d476b42](https://github.com/adcontextprotocol/adcp-client-python/commit/d476b42a721e1545c781ea57c9f1225100dd812c))
* **reporting:** report every owed period before an open revocation as withheld ([e112cc1](https://github.com/adcontextprotocol/adcp-client-python/commit/e112cc11c103fe2febbd71f13007aa3130da3a1c))
* **reporting:** serve gaps in the boundaries of the schedule they removed ([6a41458](https://github.com/adcontextprotocol/adcp-client-python/commit/6a4145829908ad7c235304fb232ad48537889b23))
* **reporting:** validate every kit buyer read except fixture buys ([d69132b](https://github.com/adcontextprotocol/adcp-client-python/commit/d69132b186984cdfffb6d34828424b9173dd9d76))
* **server:** return task and workflow handoffs uncached from IdempotencyStore.wrap ([#1794](https://github.com/adcontextprotocol/adcp-client-python/issues/1794)) ([91fe87e](https://github.com/adcontextprotocol/adcp-client-python/commit/91fe87e35195f7884c7ddb6448ab6242d948a6e4)), closes [#1781](https://github.com/adcontextprotocol/adcp-client-python/issues/1781)
* **types:** make the domain package roots lazy so a types package imports first in any order ([#1797](https://github.com/adcontextprotocol/adcp-client-python/issues/1797)) ([8331c88](https://github.com/adcontextprotocol/adcp-client-python/commit/8331c88d0a6db6befea155b676872d6503df8e5c)), closes [#1784](https://github.com/adcontextprotocol/adcp-client-python/issues/1784)

## [10.0.0-beta.5](https://github.com/adcontextprotocol/adcp-client-python/compare/v10.0.0-beta.4...v10.0.0-beta.5) (2026-10-31)


### Bug Fixes

* **compat:** decide the compact response status from the served release's schema ([#1761](https://github.com/adcontextprotocol/adcp-client-python/issues/1761)) ([ed357a7](https://github.com/adcontextprotocol/adcp-client-python/commit/ed357a7852a3a7a24ce310e1776b19a3bb60eb63))

## [10.0.0-beta.4](https://github.com/adcontextprotocol/adcp-client-python/compare/v10.0.0-beta.3...v10.0.0-beta.4) (2026-10-31)


### ⚠ BREAKING CHANGES

* **protocol:** the SDK no longer advertises or accepts AdCP 3.3-beta.2. get_supported_adcp_versions is 3.0, 3.1, 3.2, 3.3-beta.3 and 3.2-rc.7; an adcp_version pin of 3.3-beta.2 or 3.3.0-beta.2 raises ConfigurationError, and a peer that advertises only 3.3-beta.2 for the 3.3 line negotiates down to 3.2. Re-pin to 3.3-beta.3 or omit the pin. adcp.reporting.subscriptions drops project_offering, project_reporting_delivery, project_subscription_state, project_subscription_coverage and serves_settled_subscription_fields, and ReportingSubscriptionEngine.subscription_coverage no longer takes adcp_version. The generated PriorVersion, AccountsWithException and AccountsUnauthorizedItem are now ReportingSubscriptionPriorVersion, ReportingSubscriptionAccountExceptions and ReportingSubscriptionUnauthorizedAccount.
* **accounts:** AccountRegistry.ensure returns an AccountOutcome instead of an AccountRecord, billing= is optional and required only on the sync_accounts path, and a failed or rejected row is returned as AccountRejected instead of raised. AccountReady keeps account_id and status.

### Features

* **accounts:** ensure returns an AccountOutcome and routes to request_account ([406a58d](https://github.com/adcontextprotocol/adcp-client-python/commit/406a58dc062c996f370680192a086c6cb0588e61)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **client:** mark local refusals as undispatched and classify every error's recovery ([63d6ad0](https://github.com/adcontextprotocol/adcp-client-python/commit/63d6ad0722ca58282176651541964a6366161867))
* **protocol:** adopt the signed AdCP 3.3.0-beta.3 bundle ([cd92060](https://github.com/adcontextprotocol/adcp-client-python/commit/cd920600706d3dec6d66e37db9ca68afc8820d94))
* **reporting-storage:** deliver reporting subscriptions through the PostgreSQL production store ([333772f](https://github.com/adcontextprotocol/adcp-client-python/commit/333772fafca5fc14b6ee6be4d7937757ff3550d3))
* **reporting-storage:** one storage decision with versioned migrations ([c0753d6](https://github.com/adcontextprotocol/adcp-client-python/commit/c0753d6ef5a5794905892668b6b9745fc9f24dd2))
* **reporting-storage:** one storage decision with versioned migrations ([99fe10b](https://github.com/adcontextprotocol/adcp-client-python/commit/99fe10b2c4e7e434040ac53b1fc7fcf3fd8ce780)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** add the buyer write facade and guard raw sync_principal ([a161a2d](https://github.com/adcontextprotocol/adcp-client-python/commit/a161a2d0ade374eccdc66e92079423fef0030199))
* **reporting:** apply the AdCP 3.3.0-beta.3 seller rules for subscriptions ([fc81bd2](https://github.com/adcontextprotocol/adcp-client-python/commit/fc81bd28a98a0bc7f76ee49c65c9a8bb29bdaa72))
* **reporting:** apply the AdCP 3.3.0-beta.3 seller rules for subscriptions ([650d155](https://github.com/adcontextprotocol/adcp-client-python/commit/650d15584d8987e3441dd36eadfc843104f8d3f4))
* **reporting:** check reporting tasks against the current subscription grant ([d34633c](https://github.com/adcontextprotocol/adcp-client-python/commit/d34633c7945bd8119cf501238aa9f7ac0ce1d9f3))
* **reporting:** conform subscription coverage to the AdCP 3.3.0-beta.3 rules ([aefb17c](https://github.com/adcontextprotocol/adcp-client-python/commit/aefb17ca36d540904501835dafd3075e1b3b0084))
* **reporting:** conform subscription coverage to the AdCP 3.3.0-beta.3 rules ([97f555a](https://github.com/adcontextprotocol/adcp-client-python/commit/97f555ac2ff556f5e943e9c285062cd00bbb1544))
* **reporting:** deliver reporting subscriptions through production managed delivery ([6015c72](https://github.com/adcontextprotocol/adcp-client-python/commit/6015c72582809c589b3134e98fb0ac5e72ed4c7b))
* **reporting:** fail closed on a coverage period budget ([330ba77](https://github.com/adcontextprotocol/adcp-client-python/commit/330ba77f261b66ed3478c84ba9af249ebee9c5e2))
* **reporting:** fail closed on a coverage period budget ([b502e8e](https://github.com/adcontextprotocol/adcp-client-python/commit/b502e8e51a4325dd0cc78b1e451ebdca1069065b)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** hold reporting-subscription owed sets in the PostgreSQL ledger ([4808ee5](https://github.com/adcontextprotocol/adcp-client-python/commit/4808ee59f62d41c4988fefd792262838671a32dc))
* **reporting:** hold reporting-subscription owed sets in the reporting ledger ([e71bd79](https://github.com/adcontextprotocol/adcp-client-python/commit/e71bd79e31c8b5801057b7e3e9fdbf36dc3f67df))
* **reporting:** make the grant journal the one source of authorized_at ([7851c0d](https://github.com/adcontextprotocol/adcp-client-python/commit/7851c0d448f5080e7cc74ff3bcb692afb5b70cb2))
* **reporting:** make the grant journal the one source of authorized_at ([05ee4d4](https://github.com/adcontextprotocol/adcp-client-python/commit/05ee4d4bab97092284989d79d4fdefa2eaa49ab9)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** mirror the TypeScript SDK's advanced path, engine errors and readback name ([1b8f704](https://github.com/adcontextprotocol/adcp-client-python/commit/1b8f7040b8127584288b940e89d2b62a52f4f763))
* **reporting:** mirror the TypeScript SDK's advanced path, engine errors and readback name ([f5c119d](https://github.com/adcontextprotocol/adcp-client-python/commit/f5c119da9e67ecf85fe071bdef43e26cd4b63742)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** mirror the TypeScript SDK's beta.3 names and the wire regranted_at ([5925a3a](https://github.com/adcontextprotocol/adcp-client-python/commit/5925a3a6027182ce6d342fef8b1a422ebc7678f0))
* **reporting:** read deactivated subscriptions as inactive in coverage ([e840da8](https://github.com/adcontextprotocol/adcp-client-python/commit/e840da86b66f7db94b9afa9e6f5ef398f734cc47))
* **reporting:** route destinations per account as the TypeScript SDK does ([1a34cfa](https://github.com/adcontextprotocol/adcp-client-python/commit/1a34cfa20bdd6983ee3a4b6a6a517ad69f0c5e37))
* **reporting:** serve reporting subscriptions from one SubscriptionOptions block ([3288d05](https://github.com/adcontextprotocol/adcp-client-python/commit/3288d05d4abc9da30941f8110bcd2402c62a5e60))
* **reporting:** serve reporting subscriptions from ReliableReportingService ([f2559ca](https://github.com/adcontextprotocol/adcp-client-python/commit/f2559cadb50f1bb9a0617e051750315925d084b8))
* **reporting:** subscription rows carry account_id and delivery re-checks authority ([e5d1c9a](https://github.com/adcontextprotocol/adcp-client-python/commit/e5d1c9a6974078f8f31921dc7afe4cac4e61372f))
* **reporting:** use owed and list_subscription_principals, as the TypeScript SDK does ([0a4e101](https://github.com/adcontextprotocol/adcp-client-python/commit/0a4e10125c4e1bb09878bbc2dda9dbf9987d0a72))
* **reporting:** use owed and list_subscription_principals, as the TypeScript SDK does ([268789b](https://github.com/adcontextprotocol/adcp-client-python/commit/268789ba5d7e0ed73d6fd46286f929fbbdb5c692)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)


### Bug Fixes

* **accounts:** compare normalized pending actions in refresh ([7873bac](https://github.com/adcontextprotocol/adcp-client-python/commit/7873bac850877243ebe0aea6153c93c15394d4a2)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **accounts:** read requests back by request_ids and dedupe across instances ([e779518](https://github.com/adcontextprotocol/adcp-client-python/commit/e779518ae362657b125eb60a8ec70a5fc1976411)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **canonical-formats:** forward projection and narrowing repairs to next ([#1759](https://github.com/adcontextprotocol/adcp-client-python/issues/1759)) ([d005f95](https://github.com/adcontextprotocol/adcp-client-python/commit/d005f959d7b08a5b2320717ba45b50e20e7caf7d))
* **codegen:** generate the composed envelope layout of adcp[#8547](https://github.com/adcontextprotocol/adcp-client-python/issues/8547) ([#1741](https://github.com/adcontextprotocol/adcp-client-python/issues/1741)) ([9a3bc5f](https://github.com/adcontextprotocol/adcp-client-python/commit/9a3bc5fe4382c71d0952f153de943a3c14c55644))
* **codegen:** keep the controller task_id bounds and the governance corpus valid ([3b01fab](https://github.com/adcontextprotocol/adcp-client-python/commit/3b01fab83d1bffc5e7c1b3bbfd961fdc74244773))
* **decisioning:** type the managed principal task parameters ([7fc664a](https://github.com/adcontextprotocol/adcp-client-python/commit/7fc664af01fbca75995c087397b1230aa64b9e4d)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **protocol:** serve the composed envelope on compact responses at 3.3.0-beta.3 ([a81b6e4](https://github.com/adcontextprotocol/adcp-client-python/commit/a81b6e42b4a1370894ca9e62459785453e569df2))
* **reporting-storage:** keep adcp free of imports of the storage distribution ([7dc6905](https://github.com/adcontextprotocol/adcp-client-python/commit/7dc6905e75b8d9c4cc346207dc16faaa43dcb115)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** count a rejected authorized_at once and refuse buys of the wrong shape ([258b435](https://github.com/adcontextprotocol/adcp-client-python/commit/258b435fa74274ce17c3e09966b3ae42e53736f2)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** observe a served account page in one journal write ([5e23835](https://github.com/adcontextprotocol/adcp-client-python/commit/5e23835c3b29465f4faee65b97cf3c1d7304549d)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** observe a served authorization that lists no tasks ([14be044](https://github.com/adcontextprotocol/adcp-client-python/commit/14be0440fbc849e42268310657febe65ee9429f2)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** parse wire instants with 1-6 fractional digits on Python 3.10 ([8495b17](https://github.com/adcontextprotocol/adcp-client-python/commit/8495b1788803e990764942ee5d0ff7ece4d0d508)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** run write triggers off the request path and bound platform reads ([a4c9b6b](https://github.com/adcontextprotocol/adcp-client-python/commit/a4c9b6bad811405eb9f4e2e7f017303dcd6d781e)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** serve readback counts the 3.3.0-beta.3 state schema requires ([79e8960](https://github.com/adcontextprotocol/adcp-client-python/commit/79e89606873329977ba16c7df591bd94f7d2e7d7))
* **reporting:** warn on a managed offering whose rows omit account_id ([26f818a](https://github.com/adcontextprotocol/adcp-client-python/commit/26f818a402f717cb3e6d830f1c8d50168b5f6b33))
* **webhooks:** carry the task status on webhook results at 3.3.0-beta.3 ([2a965b6](https://github.com/adcontextprotocol/adcp-client-python/commit/2a965b6fcb0b0f97619853f3e45f8b0d9603193e))

## [10.0.0-beta.3](https://github.com/adcontextprotocol/adcp-client-python/compare/v10.0.0-beta.2...v10.0.0-beta.3) (2026-10-30)


### ⚠ BREAKING CHANGES

* **protocol:** the SDK no longer advertises or accepts AdCP 3.3-beta.1. get_supported_adcp_versions() is ('3.0', '3.1', '3.2', '3.3-beta.2', '3.2-rc.7'); an adcp_version pin of "3.3-beta.1" / "3.3.0-beta.1" raises ConfigurationError, and a peer that advertises only 3.3-beta.1 among the 3.3 line is negotiated like any other unserved prerelease (down to 3.2). Re-pin to 3.3-beta.2 or omit the pin. The definition-pin, purchase-rights and installment-selection types and TestControllerStore.definition_pin_probe are removed.
* **decisioning:** create_property_list / update_property_list fail with INVALID_REQUEST on mixed-case, trailing-dot or URL-shaped domain identifiers, and MCPPropertyListFetcher fails a resolution carrying one. get_products / list_products fail with INTERNAL_ERROR when a product advertises an undeclared measurement_window. get_adcp_capabilities truncates oversized publisher_domains / data_provider_domains. PrincipalService accepts "3.3" async versions by default.
* **compat:** a 3.3 request the 3.2 contract cannot express (a webhook without authentication, a minute-level daypart) now fails with ADCPVersionConversionError / UNSUPPORTED_FEATURE before sending. Earlier SDK 10 betas sent some of these to the peer invalid, for example when the request carried start_time "asap".

### Features

* **compat:** semantic 3.3 → 3.2 downgrade rules ([#1717](https://github.com/adcontextprotocol/adcp-client-python/issues/1717)) ([fab1adf](https://github.com/adcontextprotocol/adcp-client-python/commit/fab1adf1ca606dbe174820deb901d0a62333d849)), closes [#1667](https://github.com/adcontextprotocol/adcp-client-python/issues/1667)
* **decisioning:** AdCP 3.3 seller-side rules ([#1720](https://github.com/adcontextprotocol/adcp-client-python/issues/1720)) ([fbf13c9](https://github.com/adcontextprotocol/adcp-client-python/commit/fbf13c9d180e8e2a9ecd47daedba6c6b246a6bee))
* **examples:** serve the AdCP 3.3 property breakdowns from injected rows in the reference seller ([#1715](https://github.com/adcontextprotocol/adcp-client-python/issues/1715)) ([a998b82](https://github.com/adcontextprotocol/adcp-client-python/commit/a998b82242d262ad45ffded430a8386111cbceec)), closes [#1669](https://github.com/adcontextprotocol/adcp-client-python/issues/1669)
* **protocol:** adopt the signed AdCP 3.3.0-beta.2 bundle ([#1732](https://github.com/adcontextprotocol/adcp-client-python/issues/1732)) ([3e23bec](https://github.com/adcontextprotocol/adcp-client-python/commit/3e23becd8a2cb392ba9cd4df343f0a9a05456394))
* **reporting-storage:** add PostgreSQL stores for reporting subscriptions ([#1724](https://github.com/adcontextprotocol/adcp-client-python/issues/1724)) ([46a1e01](https://github.com/adcontextprotocol/adcp-client-python/commit/46a1e01a65e01d547a22a05a3858709c3309a011)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **reporting:** accept reporting subscriptions on sync_principal and track what they owe ([#1698](https://github.com/adcontextprotocol/adcp-client-python/issues/1698)) ([04238b2](https://github.com/adcontextprotocol/adcp-client-python/commit/04238b2a5a18732bddecf03ac925eccb39db2538))
* **reporting:** add buyer client helpers and a tracker for reporting subscriptions ([#1699](https://github.com/adcontextprotocol/adcp-client-python/issues/1699)) ([8a7f482](https://github.com/adcontextprotocol/adcp-client-python/commit/8a7f48206a695e3c5dc3cc7ca46aea1df3c02609))
* **reporting:** apply the AdCP 3.3 reporting rules ([#1707](https://github.com/adcontextprotocol/adcp-client-python/issues/1707)) ([3e177e6](https://github.com/adcontextprotocol/adcp-client-python/commit/3e177e69fc0fcceafc40a8854f2b7099ad905ca0)), closes [#1669](https://github.com/adcontextprotocol/adcp-client-python/issues/1669)
* **reporting:** compute owed reporting-subscription periods and their capabilities ([#1696](https://github.com/adcontextprotocol/adcp-client-python/issues/1696)) ([58e825a](https://github.com/adcontextprotocol/adcp-client-python/commit/58e825a2c0f802b85edd1eba1a9e3aa7f2f160eb))
* **reporting:** persist reporting-subscription engine state through a CAS store ([#1722](https://github.com/adcontextprotocol/adcp-client-python/issues/1722)) ([10643c7](https://github.com/adcontextprotocol/adcp-client-python/commit/10643c7fb31f22ef24956c6841c23f6315da698a)), closes [#1695](https://github.com/adcontextprotocol/adcp-client-python/issues/1695)
* **server:** seller helper for wholesale feed versions ([#1706](https://github.com/adcontextprotocol/adcp-client-python/issues/1706)) ([b014066](https://github.com/adcontextprotocol/adcp-client-python/commit/b014066004782ef312966a90ce0871ac01f2cca2)), closes [#1676](https://github.com/adcontextprotocol/adcp-client-python/issues/1676)
* **signing:** accept any well-formed adcp_use purpose in keygen ([#1708](https://github.com/adcontextprotocol/adcp-client-python/issues/1708)) ([612f97a](https://github.com/adcontextprotocol/adcp-client-python/commit/612f97af9afd66512dc97289d4e8c7f299bcc4e7))


### Bug Fixes

* **codegen:** find generated classes by name, and derive emitted envelope bases from the bundle ([#1705](https://github.com/adcontextprotocol/adcp-client-python/issues/1705)) ([50fe9e8](https://github.com/adcontextprotocol/adcp-client-python/commit/50fe9e8fc73ebc21635e3636d49fd3cbf512a35e)), closes [#1680](https://github.com/adcontextprotocol/adcp-client-python/issues/1680)
* **examples:** stop zero-filling impressions and flat multi-standard viewability in the reference seller ([#1709](https://github.com/adcontextprotocol/adcp-client-python/issues/1709)) ([a63836f](https://github.com/adcontextprotocol/adcp-client-python/commit/a63836f21dbe138d5ce021f9fab7e0a8954dbe20)), closes [#1669](https://github.com/adcontextprotocol/adcp-client-python/issues/1669)
* **media-buy-actions:** project change_term_id for AdCP 3.3 prereleases ([#1721](https://github.com/adcontextprotocol/adcp-client-python/issues/1721)) ([ac5d410](https://github.com/adcontextprotocol/adcp-client-python/commit/ac5d4100778534cfaf235846f275a05cd7d2283b)), closes [#1668](https://github.com/adcontextprotocol/adcp-client-python/issues/1668)
* **types:** defer the import-time model rebuilds that cost adcp.client 560 MiB RSS ([#1713](https://github.com/adcontextprotocol/adcp-client-python/issues/1713)) ([ab39d63](https://github.com/adcontextprotocol/adcp-client-python/commit/ab39d63e0333f94fb6879a8419e6680fdf4ccfb4))
* **webhooks:** drop aggregated_totals from media_buy_delivery webhook results ([#1716](https://github.com/adcontextprotocol/adcp-client-python/issues/1716)) ([875a4fe](https://github.com/adcontextprotocol/adcp-client-python/commit/875a4fed49cba9b92aa92d2007c6c69f3467cc94)), closes [#1669](https://github.com/adcontextprotocol/adcp-client-python/issues/1669)

## [10.0.0-beta.2](https://github.com/adcontextprotocol/adcp-client-python/compare/v10.0.0-beta.1...v10.0.0-beta.2) (2026-10-29)


### ⚠ BREAKING CHANGES

* **protocol:** the SDK no longer advertises or accepts AdCP 3.3-beta.0. get_supported_adcp_versions() is ('3.0', '3.1', '3.2', '3.3-beta.1', '3.2-rc.7'); an adcp_version pin of "3.3-beta.0" / "3.3.0-beta.0" raises ConfigurationError, and a peer that advertises only 3.3-beta.0 among the 3.3 line is negotiated like any other unserved prerelease (down to 3.2). Re-pin to 3.3-beta.1 or omit the pin.

### Features

* **protocol:** adopt the signed AdCP 3.3.0-beta.1 bundle ([#1691](https://github.com/adcontextprotocol/adcp-client-python/issues/1691)) ([b603eac](https://github.com/adcontextprotocol/adcp-client-python/commit/b603eac1ee0133cafea7ca18d899a1d26f8635af))


### Bug Fixes

* **server:** compact a pinned seller's tools/list and size the client's member bound to the byte budget ([#1701](https://github.com/adcontextprotocol/adcp-client-python/issues/1701)) ([cb2ba54](https://github.com/adcontextprotocol/adcp-client-python/commit/cb2ba54d114e6139201048862948d56992f39059)), closes [#1692](https://github.com/adcontextprotocol/adcp-client-python/issues/1692)

## [10.0.0-beta.1](https://github.com/adcontextprotocol/adcp-client-python/compare/v10.0.0-beta.0...v10.0.0-beta.1) (2026-10-29)


### ⚠ BREAKING CHANGES

* **server:** handlers on a pinned or ``supported_versions``-restricted server now receive that release instead of the packaged ``3.3-beta.0``, and ``resolve_requested_adcp_version(params)`` inside them returns it.
* **types:** AdCPBaseModel keeps unknown keys (extra='allow') instead of dropping them. A stray keyword argument on an open model goes to the wire; set ADCP_STRICT_VALIDATION=1 in tests to find them. Closed schema objects keep refusing unknown keys.
* **codegen:** model_dump() and model_dump_json() leave an unset schema-optional default off. A model built in Python no longer carries sandbox: false, timezone: "inventory_local", assignment_limit: 50 and the other schema defaults unless the caller set them; the attribute still reads the default. Pass exclude_unset=False for the SDK 9 output. Code that relied on a default reaching the peer must set it. model_dump_as_received() is deprecated. Generated root models dump with exclude_none=True.
* **types:** a received null on a null-admitting property is written back as null by model_dump(). Model(x=None), model.x = None and model_copy(update={"x": None}) leave x unset: it is omitted from the dump, from model_dump(exclude_unset=True), and from model_fields_set, so a root required-group rule no longer counts it as supplied. A None inside a dict passed to model_validate is a wire null. canonical_creative.Format refuses a JSON null on its optional properties, as its schema does.
* **types:** model_dump() and model_dump_json() write wire names for aliased fields. Code that read a dump by the Python name (dump["pass_"], dump["field_schema"]) must read the wire name (dump["pass"], dump["$schema"]); pass by_alias=False for the SDK 9 output.

### Features

* **codegen:** mark wire-required fields, and leave an unset schema default off the dump ([#1683](https://github.com/adcontextprotocol/adcp-client-python/issues/1683)) ([d0ea184](https://github.com/adcontextprotocol/adcp-client-python/commit/d0ea1840a51aee259ce0853f5176ca17252b0651))
* **types:** dump wire names by default ([#1679](https://github.com/adcontextprotocol/adcp-client-python/issues/1679)) ([9c9b1f8](https://github.com/adcontextprotocol/adcp-client-python/commit/9c9b1f80065d1e34107c1ff6d70da11e737372be))
* **types:** let the schema decide which unknown keys a model keeps ([#1687](https://github.com/adcontextprotocol/adcp-client-python/issues/1687)) ([69ccd70](https://github.com/adcontextprotocol/adcp-client-python/commit/69ccd702a0170cc99575012d15751e03d26dc6e4))
* **types:** re-emit a received null, and read a None keyword argument as unset ([#1681](https://github.com/adcontextprotocol/adcp-client-python/issues/1681)) ([c048a8c](https://github.com/adcontextprotocol/adcp-client-python/commit/c048a8c0a8e77b4562551ba35b8b6d2937c1db2a))


### Bug Fixes

* **server:** let a server's version pin set the release its handlers run on ([#1689](https://github.com/adcontextprotocol/adcp-client-python/issues/1689)) ([24d40e0](https://github.com/adcontextprotocol/adcp-client-python/commit/24d40e0127a0236694f3b261aed74a35fca2c05e))

## [10.0.0-beta.0](https://github.com/adcontextprotocol/adcp-client-python/compare/v9.0.0-beta.3...v10.0.0-beta.0) (2026-10-27)


### ⚠ BREAKING CHANGES

* **protocol:** the wire pin is AdCP 3.3.0-beta.0 (adcp_version "3.3-beta.0" by default; pin adcp_version="3.2" for a 3.2 contract). adcp.types.UniversalMacro is a RootModel union; the closed enum is UniversalMacro3. The identity preview modules are removed and adcp.types.experimental_identity binds the generated 3.3 types (RequestSigning.profiles is list[Profile], PrincipalChangedWebhook.reason is the generated enum). adcp.signing.identity_schema_bundle is removed. Legacy RFC 9421 SigningConfig / RequestSignatureVerification construction emits a DeprecationWarning. evaluator_id is gone from evaluator specs. See MIGRATION_v9_to_v10.md.
* **a2a:** With request signing configured, A2A requests that break the AdCP 3.3 operation-resolution hardening clauses (duplicate or case-variant keys, batches, malformed Parts, extra DataParts, mismatched task continuations) are refused with request_body_malformed or INVALID_REQUEST.
* **webhooks:** WebhookReceiver rejects bodies over 1 MiB (was 10 MiB) and JSON deeper than 64 or with more than 100,000 members with HTTP 413 before signature verification, and adds the rejection reasons body_too_complex, content_encoding_unsupported and body_framing_invalid. Client responses past the depth or member bounds raise ADCPResponseTooComplexError.
* **signing:** BrandJsonJwksResolver and the other brand.json fetchers raise ValueError for max_body_bytes above 2 MiB, the AdCP 3.3 cap verifiers must not read past.
* **canonical-formats:** PixelTrackerDowngrade.v1 is now V1UrlTracker or None. Check it before reading asset_id or url. See MIGRATION_v9_to_v10.md.
* **reporting:** the PostgreSQL reporting stores, the row storage providers, the change feed, the ownership-migration helpers and `ReliableReportingService.postgres` leave `adcp`; install `adcp-reporting-storage[pg]` (plus `gcs`/`s3`/`azure`) and import them from `adcp_reporting_storage`. The `adcp[reporting-gcs]`, `[reporting-s3]` and `[reporting-azure]` extras are removed. See docs/reporting-storage-package-migration.md.
* **codegen:** a JSON document carrying `null` for an optional property whose schema does not admit `null` is now refused by `model_validate_json` and `TypeAdapter.validate_json`, and the published JSON Schema of such a property has no null arm. A Python `None` is still accepted. JSON produced with `exclude_none=False` must be read back in Python mode (`model_validate(json.loads(...))`).
* **types:** SyncCreativesRequest.creatives is now list[CreativeAsset] | None. Handlers must guard it before iteration or use request.creatives or [] for assignment-only requests.
* **signing:** adcp.signing.b64url_decode accepts only RFC 7515 base64url (no padding, no standard-alphabet characters, no non-zero trailing bits) and raises ValueError for anything else. public_key_from_jwk reads a JWK through joserfc, so its ValueError text changes. private_key_from_jwk raises ValueError when a JWK's x (or y) isn't the public key of its d. adcp.signing.publisher_pins.jwk_thumbprint returns the RFC 7638 thumbprint as a base64url str instead of the digest bytes.
* **types:** adcp.types.generated_poc no longer imports. Use adcp.types.domains, or run adcp migrate v3-to-v4 --apply.
* **reporting:** row storage tables, provider inputs and locators are renamed to the AdCP names above, REPORTING_ROW_ENCODING_V1 is replaced by REPORTING_ROW_FORMAT_V1 and REPORTING_ROW_ALGORITHM_V1, and {period_date} renders the source timezone date as YYYY-MM-DD instead of the UTC date as yyyy/MM/dd, and a custom key template without {revision_key} is refused unless its binding is read_only. create_schema migrates existing databases; stop older writers first.

### ci

* **release:** cut 10.0.0-alpha.N pre-releases from next ([df3ee2c](https://github.com/adcontextprotocol/adcp-client-python/commit/df3ee2c1221169ba1cd9df425a8c5be250c53e9a))
* **release:** cut 10.0.0-beta.N pre-releases from next ([#1672](https://github.com/adcontextprotocol/adcp-client-python/issues/1672)) ([322e6f0](https://github.com/adcontextprotocol/adcp-client-python/commit/322e6f01731108e3f8fa7ff3c5216fbd9825c2ff))


### chore

* **release:** leave the beta channel for SDK 9.0.0 GA ([#1567](https://github.com/adcontextprotocol/adcp-client-python/issues/1567)) ([4e9c123](https://github.com/adcontextprotocol/adcp-client-python/commit/4e9c1231600335bb45acada61523050b77dccfc5))


### Features

* **client:** cap agent response size by default ([#1504](https://github.com/adcontextprotocol/adcp-client-python/issues/1504)) ([d5195a9](https://github.com/adcontextprotocol/adcp-client-python/commit/d5195a9525239124416741808addcbcf0f0a8a98))
* **compat:** negotiate and translate AdCP minor versions ([#1559](https://github.com/adcontextprotocol/adcp-client-python/issues/1559)) ([31d7217](https://github.com/adcontextprotocol/adcp-client-python/commit/31d7217e81533e6513cfe4ab28441abb0ed3db77))
* **decisioning:** export media-buy terminal statuses and the observed transition graph ([#1664](https://github.com/adcontextprotocol/adcp-client-python/issues/1664)) ([4b1554e](https://github.com/adcontextprotocol/adcp-client-python/commit/4b1554e14de0e72bf21af8a6a2b3f102a1227a5b)), closes [#1604](https://github.com/adcontextprotocol/adcp-client-python/issues/1604)
* **decisioning:** resolve account_id references in NaturalKeyAccounts for AdCP 3.3 ([#1653](https://github.com/adcontextprotocol/adcp-client-python/issues/1653)) ([dca87b9](https://github.com/adcontextprotocol/adcp-client-python/commit/dca87b95257a64ddf35197eb99fe733740b4b0b7)), closes [#1478](https://github.com/adcontextprotocol/adcp-client-python/issues/1478)
* **identity:** add experimental AdCP 3.3 agent identity ([#1502](https://github.com/adcontextprotocol/adcp-client-python/issues/1502)) ([3961d1c](https://github.com/adcontextprotocol/adcp-client-python/commit/3961d1cf7a235c4d45673370521b1aec9bc4c496))
* **identity:** verify JWS artifacts on the issuer's Web Bot Auth key directory ([#1543](https://github.com/adcontextprotocol/adcp-client-python/issues/1543)) ([233ebee](https://github.com/adcontextprotocol/adcp-client-python/commit/233ebeefd1bd44640a9fa983299c6a65410b2356))
* **media-buy:** persist and echo create context on get_media_buys ([#1600](https://github.com/adcontextprotocol/adcp-client-python/issues/1600)) ([4083dc5](https://github.com/adcontextprotocol/adcp-client-python/commit/4083dc52702ddd1d20aa840dd4265bf61dd75ddd)), closes [#1575](https://github.com/adcontextprotocol/adcp-client-python/issues/1575)
* **property-lists:** unify resolution and validate seller inventory ([#1532](https://github.com/adcontextprotocol/adcp-client-python/issues/1532)) ([734bf3a](https://github.com/adcontextprotocol/adcp-client-python/commit/734bf3acde31fb1ea7b96867710c08087384737d))
* **protocol:** adopt the signed AdCP 3.3.0-beta.0 bundle ([#1662](https://github.com/adcontextprotocol/adcp-client-python/issues/1662)) ([7472aae](https://github.com/adcontextprotocol/adcp-client-python/commit/7472aae088de21ea77b27c4d796d72cacde0751c))
* **reporting:** add current-revision host reads ([#1473](https://github.com/adcontextprotocol/adcp-client-python/issues/1473)) ([13ca2c5](https://github.com/adcontextprotocol/adcp-client-python/commit/13ca2c502818777bf1eef2f2cd5e8bfbac196dcf))
* **reporting:** add GCS, S3 and Azure Blob row providers ([#1468](https://github.com/adcontextprotocol/adcp-client-python/issues/1468)) ([fde127d](https://github.com/adcontextprotocol/adcp-client-python/commit/fde127db8992bc6f62303ebbfcbf8f0617be33f4))
* **reporting:** add the shared host change feed and registered consumers ([#1472](https://github.com/adcontextprotocol/adcp-client-python/issues/1472)) ([b7012e0](https://github.com/adcontextprotocol/adcp-client-python/commit/b7012e05591f3e7823314ab39d65cb1cc723324a))
* **reporting:** align row storage with AdCP stored names and the period date rule ([#1485](https://github.com/adcontextprotocol/adcp-client-python/issues/1485)) ([47084e8](https://github.com/adcontextprotocol/adcp-client-python/commit/47084e83a4a2cfd879db4127e26c3fb54fd73dd8))
* **reporting:** migrate legacy inline rows to verified chunks ([#1466](https://github.com/adcontextprotocol/adcp-client-python/issues/1466)) ([bf7e3a6](https://github.com/adcontextprotocol/adcp-client-python/commit/bf7e3a62b38975ed037bc8dbd440bfb2ebe440d4))
* **reporting:** retire expired periods with tombstones and holds ([#1474](https://github.com/adcontextprotocol/adcp-client-python/issues/1474)) ([c527b18](https://github.com/adcontextprotocol/adcp-client-python/commit/c527b1866bf544ca0926eb844b316136a2172a5c))
* **reporting:** schedule retention in the production worker ([#1476](https://github.com/adcontextprotocol/adcp-client-python/issues/1476)) ([5c23598](https://github.com/adcontextprotocol/adcp-client-python/commit/5c23598b420725987c3ed0f79c923ead19f1d6be))
* **reporting:** ship the Reliable Reporting storage as adcp-reporting-storage ([#1557](https://github.com/adcontextprotocol/adcp-client-python/issues/1557)) ([d59eee9](https://github.com/adcontextprotocol/adcp-client-python/commit/d59eee9da4d246557bfacf42119729e1d68e78a7))
* **reporting:** store revision rows as verified chunks in PostgreSQL ([#1464](https://github.com/adcontextprotocol/adcp-client-python/issues/1464)) ([54a133e](https://github.com/adcontextprotocol/adcp-client-python/commit/54a133ed8e1723e20f1d575b68af88e214afede3))
* **reporting:** sweep abandoned uploads and prune expired row sets ([#1469](https://github.com/adcontextprotocol/adcp-client-python/issues/1469)) ([e32daba](https://github.com/adcontextprotocol/adcp-client-python/commit/e32daba5c78b5c49ebeee25730b42acb0a1c72d6))
* **reporting:** sync empty-ledger replacement and owned-ledger rollback for 9.0 adopters ([#1554](https://github.com/adcontextprotocol/adcp-client-python/issues/1554)) ([38d046a](https://github.com/adcontextprotocol/adcp-client-python/commit/38d046a809bab9c684d8c0cac761f8d59a50072d))
* **reporting:** write row chunks to object storage with fenced uploads ([#1467](https://github.com/adcontextprotocol/adcp-client-python/issues/1467)) ([83f62c9](https://github.com/adcontextprotocol/adcp-client-python/commit/83f62c9bcef7cca65abae376fe796e6df13eb064))
* **server:** let PrincipalResolverError carry a stable error code ([#1633](https://github.com/adcontextprotocol/adcp-client-python/issues/1633)) ([cf96d57](https://github.com/adcontextprotocol/adcp-client-python/commit/cf96d573ba2240fb5b666dc0f891378f253dfbe4)), closes [#1623](https://github.com/adcontextprotocol/adcp-client-python/issues/1623)
* **signing:** accept brand.json up to 2 MiB for AdCP 3.3 ([#1654](https://github.com/adcontextprotocol/adcp-client-python/issues/1654)) ([ca8d422](https://github.com/adcontextprotocol/adcp-client-python/commit/ca8d42239dfe5b51d0004ca9771ab741c26f5d8c)), closes [#1635](https://github.com/adcontextprotocol/adcp-client-python/issues/1635)
* **signing:** pin the Public Suffix List snapshot used for eTLD+1 ([#1652](https://github.com/adcontextprotocol/adcp-client-python/issues/1652)) ([a6fd1fa](https://github.com/adcontextprotocol/adcp-client-python/commit/a6fd1fa4cad2e842287e8f7026199fa45ae28dfa)), closes [#1562](https://github.com/adcontextprotocol/adcp-client-python/issues/1562)
* **types:** keep wire bytes on parsed URLs, datetimes, UUIDs and emails so digests round-trip ([#1617](https://github.com/adcontextprotocol/adcp-client-python/issues/1617)) ([a7f5acd](https://github.com/adcontextprotocol/adcp-client-python/commit/a7f5acdbf58fcba5c2e590976c00dba4b84dbbf2)), closes [#1610](https://github.com/adcontextprotocol/adcp-client-python/issues/1610)
* **types:** remove the adcp.types.generated_poc alias in v10 ([#1536](https://github.com/adcontextprotocol/adcp-client-python/issues/1536)) ([893720b](https://github.com/adcontextprotocol/adcp-client-python/commit/893720b649bf5ff5b7ac45d5e5c2c724469fbd98))
* **webhooks:** add PgNotificationOutbox.enqueue_prepared_sync for synchronous business transactions ([#1665](https://github.com/adcontextprotocol/adcp-client-python/issues/1665)) ([e081fe4](https://github.com/adcontextprotocol/adcp-client-python/commit/e081fe431d83c733648921d34ead9420d0cbce4b)), closes [#1607](https://github.com/adcontextprotocol/adcp-client-python/issues/1607)
* **webhooks:** bound inbound bodies before verification and response JSON shape for AdCP 3.3 ([#1656](https://github.com/adcontextprotocol/adcp-client-python/issues/1656)) ([283f877](https://github.com/adcontextprotocol/adcp-client-python/commit/283f877de119cd75a5624237ea2ef67e0c1aa7c6)), closes [#1636](https://github.com/adcontextprotocol/adcp-client-python/issues/1636)


### Bug Fixes

* **a2a:** echo request context on unknown-skill and unexpected-error tasks ([#1597](https://github.com/adcontextprotocol/adcp-client-python/issues/1597)) ([7b44d8d](https://github.com/adcontextprotocol/adcp-client-python/commit/7b44d8d5d1627f98fede0ed5660ab3d804f230bd)), closes [#1573](https://github.com/adcontextprotocol/adcp-client-python/issues/1573)
* **a2a:** enforce the AdCP 3.3 operation-resolution hardening clauses ([#1659](https://github.com/adcontextprotocol/adcp-client-python/issues/1659)) ([b3e34c2](https://github.com/adcontextprotocol/adcp-client-python/commit/b3e34c2ff52770b53e0460c8ff4eb3982e71418b)), closes [#1637](https://github.com/adcontextprotocol/adcp-client-python/issues/1637)
* **a2a:** keep integers integral in echoed context across the protobuf Struct ([#1594](https://github.com/adcontextprotocol/adcp-client-python/issues/1594)) ([bf6d966](https://github.com/adcontextprotocol/adcp-client-python/commit/bf6d96641910ccdaa2e2b95bf7965a564c9e1d9b)), closes [#1572](https://github.com/adcontextprotocol/adcp-client-python/issues/1572)
* **adagents:** strip surrounding whitespace in normalize_url ([#1629](https://github.com/adcontextprotocol/adcp-client-python/issues/1629)) ([4e2ae8f](https://github.com/adcontextprotocol/adcp-client-python/commit/4e2ae8fc153a662ec67bbb24ef478621b37570db)), closes [#1624](https://github.com/adcontextprotocol/adcp-client-python/issues/1624)
* **canonical-formats:** compare every parameter a format reference declares ([#1518](https://github.com/adcontextprotocol/adcp-client-python/issues/1518)) ([d073712](https://github.com/adcontextprotocol/adcp-client-python/commit/d0737127b94b58355221c3ab9b1e7353e66ac2d6)), closes [#1517](https://github.com/adcontextprotocol/adcp-client-python/issues/1517)
* **canonical-formats:** drop playback trackers on the v1 downgrade instead of firing them as impressions ([#1655](https://github.com/adcontextprotocol/adcp-client-python/issues/1655)) ([2ffa538](https://github.com/adcontextprotocol/adcp-client-python/commit/2ffa5380b3bff2a8983447efd8c48dde50964a7a)), closes [#1643](https://github.com/adcontextprotocol/adcp-client-python/issues/1643)
* **canonical-formats:** read an omitted pixel_ratio as 1x on the supported side too ([#1553](https://github.com/adcontextprotocol/adcp-client-python/issues/1553)) ([b07adbc](https://github.com/adcontextprotocol/adcp-client-python/commit/b07adbc59c66cef8a7ada8189ab7739fc77c7733))
* **codegen:** refuse a JSON null on optional properties whose schema forbids one ([#1555](https://github.com/adcontextprotocol/adcp-client-python/issues/1555)) ([a40c2f0](https://github.com/adcontextprotocol/adcp-client-python/commit/a40c2f0a71f9110cedc844f125630d3055e209f4))
* **compat:** infer display_tag and every 3.2 asset type from the asset key ([#1628](https://github.com/adcontextprotocol/adcp-client-python/issues/1628)) ([88cf04a](https://github.com/adcontextprotocol/adcp-client-python/commit/88cf04a1cd684595b6ae58712cec42e01590c58f)), closes [#1624](https://github.com/adcontextprotocol/adcp-client-python/issues/1624)
* **compat:** log server-side conversion losses at DEBUG with one INFO summary ([#1609](https://github.com/adcontextprotocol/adcp-client-python/issues/1609)) ([f7e4f3a](https://github.com/adcontextprotocol/adcp-client-python/commit/f7e4f3a5f1c8acba40072df0af377927a0af74ec))
* **compat:** never downgrade into a payload the older schema refuses ([#1630](https://github.com/adcontextprotocol/adcp-client-python/issues/1630)) ([b5234e2](https://github.com/adcontextprotocol/adcp-client-python/commit/b5234e221b539abb7c6797aa278e9c2874ea38fa))
* **decisioning:** project ADCPTaskError from platform methods to its own wire error ([#1632](https://github.com/adcontextprotocol/adcp-client-python/issues/1632)) ([7b95b26](https://github.com/adcontextprotocol/adcp-client-python/commit/7b95b26f0d61b1b35edaae1d192f8f74adc40289)), closes [#1622](https://github.com/adcontextprotocol/adcp-client-python/issues/1622)
* **governance:** require crit markers with their claims, and run the signing tests CI skips ([#1539](https://github.com/adcontextprotocol/adcp-client-python/issues/1539)) ([15c85cf](https://github.com/adcontextprotocol/adcp-client-python/commit/15c85cf6e5de6b1b401c688d3e3e8470a47ba449))
* **identity:** a grantee that is not a registrable domain covers no claim ([#1542](https://github.com/adcontextprotocol/adcp-client-python/issues/1542)) ([6cb3c8a](https://github.com/adcontextprotocol/adcp-client-python/commit/6cb3c8a2be7823b3f399cd7351dfa62154439d2c))
* **identity:** align AdCP 3.3 agent identity with the final spec and the Go and TS SDKs ([#1514](https://github.com/adcontextprotocol/adcp-client-python/issues/1514)) ([2b08ced](https://github.com/adcontextprotocol/adcp-client-python/commit/2b08cedcf5b75e7a364363e1cfcea676bd241456))
* **identity:** implicit grant authority extends to the brand's house ([#1565](https://github.com/adcontextprotocol/adcp-client-python/issues/1565)) ([4b8b7e4](https://github.com/adcontextprotocol/adcp-client-python/commit/4b8b7e48ce9a695431c57317adbcbb77ee01d220))
* **identity:** never send a bound origin to the 3.2 profile, and treat a missing kid as invalid ([#1549](https://github.com/adcontextprotocol/adcp-client-python/issues/1549)) ([2db4741](https://github.com/adcontextprotocol/adcp-client-python/commit/2db47417806c8e66ebf9c59c4c279be15db586df))
* **protocol:** adopt signed AdCP 3.2.4 on SDK 9 beta ([#1479](https://github.com/adcontextprotocol/adcp-client-python/issues/1479)) ([0ad2a8f](https://github.com/adcontextprotocol/adcp-client-python/commit/0ad2a8fae218ae927b75ed6ac38ddb5107822350))
* **protocol:** adopt signed AdCP 3.2.7 on SDK 9 ([#1657](https://github.com/adcontextprotocol/adcp-client-python/issues/1657)) ([95ed163](https://github.com/adcontextprotocol/adcp-client-python/commit/95ed163d61c40a851997280c647ef8627ada17b0))
* **release:** tighten V9 reporting and CI readiness checks ([#1550](https://github.com/adcontextprotocol/adcp-client-python/issues/1550)) ([41c2be7](https://github.com/adcontextprotocol/adcp-client-python/commit/41c2be73e6851264f71edbab13fcd801d2a94454)), closes [#1383](https://github.com/adcontextprotocol/adcp-client-python/issues/1383) [#1493](https://github.com/adcontextprotocol/adcp-client-python/issues/1493)
* **reporting:** answer unreadable exact revision reads with SERVICE_UNAVAILABLE ([#1471](https://github.com/adcontextprotocol/adcp-client-python/issues/1471)) ([623b2c1](https://github.com/adcontextprotocol/adcp-client-python/commit/623b2c1603d2c59dc3d5429f7e44c469a3550fcb))
* **reporting:** classify pending status and owed history as transient, unresolved references as REFERENCE_NOT_FOUND ([#1631](https://github.com/adcontextprotocol/adcp-client-python/issues/1631)) ([a2ec81c](https://github.com/adcontextprotocol/adcp-client-python/commit/a2ec81c0679042c0a3cb0a0779d03df53311e174)), closes [#1618](https://github.com/adcontextprotocol/adcp-client-python/issues/1618)
* **reporting:** keep feed consumer checkpoints forward-only ([#1515](https://github.com/adcontextprotocol/adcp-client-python/issues/1515)) ([0de3f57](https://github.com/adcontextprotocol/adcp-client-python/commit/0de3f57c7e3a434c6d774e880663ca8c4908841c)), closes [#1509](https://github.com/adcontextprotocol/adcp-client-python/issues/1509)
* **reporting:** keep retention moving past stuck work and make it visible ([#1482](https://github.com/adcontextprotocol/adcp-client-python/issues/1482)) ([4623104](https://github.com/adcontextprotocol/adcp-client-python/commit/462310439d05b92cdfe510b9aac571a8a9f28f0c))
* **reporting:** name the cause of a reporting startup failure ([a202a3c](https://github.com/adcontextprotocol/adcp-client-python/commit/a202a3cbeb41ce6f89dcac8db943a0a0f9d72da3)), closes [#1561](https://github.com/adcontextprotocol/adcp-client-python/issues/1561)
* **reporting:** reject a change page that crossed a concurrent prune ([#1516](https://github.com/adcontextprotocol/adcp-client-python/issues/1516)) ([73d1261](https://github.com/adcontextprotocol/adcp-client-python/commit/73d12611effa9adeb8a588992428a60864e6a006)), closes [#1508](https://github.com/adcontextprotocol/adcp-client-python/issues/1508)
* **reporting:** truncate account feed pages at the first in-flight change and match JS backoff ([#1483](https://github.com/adcontextprotocol/adcp-client-python/issues/1483)) ([21f4216](https://github.com/adcontextprotocol/adcp-client-python/commit/21f4216addc5e39f30b076288889b57f8d1cb455))
* **server:** advertise compact output schemas so discovery is servable ([#1507](https://github.com/adcontextprotocol/adcp-client-python/issues/1507)) ([d908aaa](https://github.com/adcontextprotocol/adcp-client-python/commit/d908aaa98caa88e5b0cd992037eb5010dc009da1))
* **server:** collect stranded defs, memoize the escape hatch, soften a claim ([#1513](https://github.com/adcontextprotocol/adcp-client-python/issues/1513)) ([fc285b0](https://github.com/adcontextprotocol/adcp-client-python/commit/fc285b057406f0e715412d1d53bb86a281503fb2)), closes [#1507](https://github.com/adcontextprotocol/adcp-client-python/issues/1507)
* **server:** mirror a raised AdcpError into the payload errors array ([#1592](https://github.com/adcontextprotocol/adcp-client-python/issues/1592)) ([cd0319b](https://github.com/adcontextprotocol/adcp-client-python/commit/cd0319b57d27ab8bbb06ddb091537645dadf887b)), closes [#1558](https://github.com/adcontextprotocol/adcp-client-python/issues/1558)
* **signing:** match standard Web Bot Auth, and finish the library migration ([#1537](https://github.com/adcontextprotocol/adcp-client-python/issues/1537)) ([09db189](https://github.com/adcontextprotocol/adcp-client-python/commit/09db1893c7b45ca2ca07d6a8db90bb59f05ec089))
* **signing:** use only the directory keys with a valid signature ([#1544](https://github.com/adcontextprotocol/adcp-client-python/issues/1544)) ([56d5e73](https://github.com/adcontextprotocol/adcp-client-python/commit/56d5e7329ff8437933c9cc720520fb78d8427881))
* **signing:** work with http-sf 1.5.0 and http-message-signatures 2.2.0 ([#1568](https://github.com/adcontextprotocol/adcp-client-python/issues/1568)) ([cf97bd2](https://github.com/adcontextprotocol/adcp-client-python/commit/cf97bd26253fcae0c59573bcf33f6f9fb138ee5c))
* **sync:** inherit generated fields on the identity facades and skip the retired generated_poc alias test ([7d8b043](https://github.com/adcontextprotocol/adcp-client-python/commit/7d8b0439e3165ce5d0107a9ba97ae867751bf708))
* **targeting:** bridge package forecast request inputs ([#1598](https://github.com/adcontextprotocol/adcp-client-python/issues/1598)) ([7cd667f](https://github.com/adcontextprotocol/adcp-client-python/commit/7cd667f9e134d958aadffadef1420d0c91c941ef))
* **tasks:** echo the poll's own context on tasks/get ([#1599](https://github.com/adcontextprotocol/adcp-client-python/issues/1599)) ([f1f7593](https://github.com/adcontextprotocol/adcp-client-python/commit/f1f75930152510de374d98bd286f2462af2100bb)), closes [#1574](https://github.com/adcontextprotocol/adcp-client-python/issues/1574)
* **testing:** stop build_all_models hanging after a generated_poc alias import ([#1626](https://github.com/adcontextprotocol/adcp-client-python/issues/1626)) ([6f61234](https://github.com/adcontextprotocol/adcp-client-python/commit/6f61234cc2717e5e9cff2a824b28d3eb480af6f8)), closes [#1619](https://github.com/adcontextprotocol/adcp-client-python/issues/1619)
* **types:** allow assignment-only creative syncs ([81c2a8b](https://github.com/adcontextprotocol/adcp-client-python/commit/81c2a8b2b0fb221fff3d5d8ddcb96c673b9fbe68)), closes [#1545](https://github.com/adcontextprotocol/adcp-client-python/issues/1545)
* **types:** drop the empty-model arm codegen emits beside a constrained array ([#1658](https://github.com/adcontextprotocol/adcp-client-python/issues/1658)) ([adaf29b](https://github.com/adcontextprotocol/adcp-client-python/commit/adaf29bcb040860d8d4047741735002ea527016f)), closes [#1616](https://github.com/adcontextprotocol/adcp-client-python/issues/1616)
* **types:** generate the open asset unions, and derive the vocabulary they route by ([#1501](https://github.com/adcontextprotocol/adcp-client-python/issues/1501)) ([30a72ee](https://github.com/adcontextprotocol/adcp-client-python/commit/30a72ee2a409bccb53fe961816584bdea2518f70))
* **types:** inherit the generated constraints on the canonical creatives field ([#1548](https://github.com/adcontextprotocol/adcp-client-python/issues/1548)) ([69a0f6b](https://github.com/adcontextprotocol/adcp-client-python/commit/69a0f6b9525b400b17b25c1b18294a4650c56afb))
* **types:** inherit the generated creatives ceiling on PackageRequest ([#1579](https://github.com/adcontextprotocol/adcp-client-python/issues/1579)) ([6f13ce0](https://github.com/adcontextprotocol/adcp-client-python/commit/6f13ce07e69bc24de23ce63b97958875f6866cc7))
* **types:** inherit the generated field on every hand-written redeclaration ([#1625](https://github.com/adcontextprotocol/adcp-client-python/issues/1625)) ([6e0ed68](https://github.com/adcontextprotocol/adcp-client-python/commit/6e0ed688e16c3be94fb117fef5157b4cda08a136))
* **types:** leave unset optional fields off the wire from any parent ([#1591](https://github.com/adcontextprotocol/adcp-client-python/issues/1591)) ([1086cd9](https://github.com/adcontextprotocol/adcp-client-python/commit/1086cd90f70e760873b445f15db5cdcb0d8aeeed)), closes [#1587](https://github.com/adcontextprotocol/adcp-client-python/issues/1587)
* **types:** model macro_resolution_capabilities on Format ([#1505](https://github.com/adcontextprotocol/adcp-client-python/issues/1505)) ([eb66de8](https://github.com/adcontextprotocol/adcp-client-python/commit/eb66de84c7445abe0de185947d886290b08eb14f))
* **types:** preserve custom targeting validation errors ([#1484](https://github.com/adcontextprotocol/adcp-client-python/issues/1484)) ([275e520](https://github.com/adcontextprotocol/adcp-client-python/commit/275e520822eaa280f28965ade8e20085f78cfd57))
* **types:** refuse a JSON null on reporting_webhook.operation_id too ([#1570](https://github.com/adcontextprotocol/adcp-client-python/issues/1570)) ([9e238f3](https://github.com/adcontextprotocol/adcp-client-python/commit/9e238f34f75870d4d27b07156e26093a11b30151))
* **webhooks:** echo reporting_webhook.operation_id on scheduled delivery reports ([#1552](https://github.com/adcontextprotocol/adcp-client-python/issues/1552)) ([0bac033](https://github.com/adcontextprotocol/adcp-client-python/commit/0bac033511d5b73908f2720d670b84a2642d2dba)), closes [#1092](https://github.com/adcontextprotocol/adcp-client-python/issues/1092)
* **webhooks:** send PgWebhookDeliverySupervisor task webhooks and deliver the ones already stuck in the queue ([#1651](https://github.com/adcontextprotocol/adcp-client-python/issues/1651)) ([83a2fe3](https://github.com/adcontextprotocol/adcp-client-python/commit/83a2fe37e8517950c4aa465ad0d5b92a955bf16c))


### Performance Improvements

* **types:** reduce canonical creative serialization overhead ([#1521](https://github.com/adcontextprotocol/adcp-client-python/issues/1521)) ([bde770b](https://github.com/adcontextprotocol/adcp-client-python/commit/bde770b76ab9c07fd6e8bb11394dd868ddb09984))


### Reverts

* move experimental AdCP 3.3 identity from main to next ([#1534](https://github.com/adcontextprotocol/adcp-client-python/issues/1534)) ([1e6d4b9](https://github.com/adcontextprotocol/adcp-client-python/commit/1e6d4b99b5340a5862e2429425354a1b70c27e48))
