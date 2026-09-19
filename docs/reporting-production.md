# Production reporting and versioned status

`adcp.reporting.production` composes the existing durable producer, materializer,
receipt ingress and frozen feed with versioned private status. Use
[`examples/reporting_production.py`](../examples/reporting_production.py) for the
typed composition and authenticated MCP/A2A lifecycle. Existing Core polling
and eligible Core notification deployments keep their existing composition.

## Provider and source contracts

Each `ReportingProductionOffering` binds a complete public offering to one
actual `ReportingProducer`, source offering and installed verifier. A
`ReportingProductionDestination` supplies complete `ReportingProductionMethod`
values and resolves each authorized destination into a
`ReportingProductionDestinationBinding`. Pattern or transport labels alone
are insufficient: provider, destination modes, access mode, orchestration,
reader compatibility and the selected destination must match. Supply opaque
references, never credentials or bearer URLs. The same provider resolves
fresh, separately authorized write and readback sessions.

The source implements `ReportingProductionSource.configuration_binding()`.
Construct its result with `ReportingProductionSourceBinding.for_configuration()`
using the authenticated account/catalog mapping and effective source capability
digest. Every media-buy/product pair is explicit. Report-definition IDs do not
identify products. Account, configuration ID and generation are all part of the
binding. Multiple offerings can share a definition; they still need distinct
applicable source contracts. Supported-offering discovery can precede the first
account binding.

Admission durably freezes the source mapping, generation semantics and selected
provider method. Source acquisition and materialization compare the current
authoritative contracts to those frozen values. Withdrawal or incompatible
mutation fails closed; it cannot reinterpret an old generation. Resolve and
admit a new generation for a different contract. Historical pages and accepted
receipt evidence retain their original values.

`ReportingProductionConfigurationTask` wraps the adopter's authenticated account
task. Its callback receives an `admit` function; call it with a
`ReportingConfigurationAdmission` containing the complete requested public
configuration and trusted resolved records before returning ready or inactive
state, including replay. The SDK checks actual schedule, scope, provider method,
finality, source generation and returned coverage. It completes the account's
versioned activation before returning ready, so newly admitted accounts need no
separate account-enumeration worker. Account provisioning and its durable
idempotency remain the account implementation's responsibility; do not claim
unsupported account features in its capability model. This producer admits
explicit full media-buy scopes and fixed-duration schedules that it can prove.
It does not turn an unsupported dynamic or partial scope into full coverage.

Managed delivery-only polling needs the source, durable writer, verified
readback, complete status/exact reads and configuration route. It does not need
the receipt route or an HTTP notification worker. Reconciled additionally needs
official finality, canonical verification, and mounted revision/adjustment
receipt ingress. Neither the development writer nor the in-memory conformance
store advertises production durability. Capability checks bind the actual
mounted handler and running components; adopters do not maintain readiness
booleans. Protected capability fields cannot be supplied through `extra=`.

## Migration, drain and activation

1. Stop and drain autonomous legacy materializer writers. Resolve or explicitly
   import uncertain legacy effects using the
   [materializer recovery procedure](reporting-durable-materializer.md). Keep the
   original external idempotency identity for pending SDK attempts.
2. Stop and drain old status projectors and clock sweepers. Ordinary compatible
   historical readers/writers are a different compatibility claim from running
   those incompatible autonomous workers.
3. Call `await store.create_schema()` on `PgReportingProductionStore` before
   starting workers. Bootstrap remains transactional, serialized and repeatable.
   Keep the private configuration lease-fairness objects: they deliberately
   remain outside the old mandatory manifests.
4. Construct the verifier registry, trusted source/provider adapters, producer,
   materializer, `PgReportingStatusProjection` and `ReportingProductionSupport`.
   Mount **that support's handler** on the authenticated transports before
   `await support.start()`. The typed example uses the combined server's startup
   and shutdown hooks.
5. Activate existing accounts with `await support.activate(account_id=...)`, or
   let validated configuration admission activate the affected account. Activation
   fences old projection writers and incrementally consumes preserved captured
   boundaries and baselines. An interruption resumes those original inputs;
   it does not reconstruct historical status from today's records.

The isolated projection and production manifests do not append objects to the
older ledger, materializer, receipt-ingestion or frozen-feed manifests. Old or
partial installations cannot prove production readiness. Catalog proofs are
positive caches scoped to the concrete support/pool and invalidation epoch;
live component and route checks still run. For later DDL, stop/drain support,
migrate, construct fresh support, validate, and restart. Arbitrary serving-time
DDL or search-path mutation is not automatically detected. Bound database
statement execution at the adopter/database layer.
The support also binds the pool's concrete identity and checks its open state
on each readiness request. Closing or replacing that pool withdraws the claim
even while the immutable catalog proof remains cached; rebuild the support for
a new pool.
The projection queue and all configured notification queues must retain the
same pool as the ledger. Warm discovery performs no pool checkout and stays
available when that valid pool is temporarily saturated.

Activation never promotes epoch-zero work or readiness. Pending attempts keep
their original work identity, epoch and permanently quarantined events through
resume, replay and restart. Only genuinely new qualified work enters the
production epoch. The verified terminal result, immutable status capture, work
ACK and enabled logical `reporting.delivery_ready` enqueue share one transaction.
Enqueue failure rolls all of them back. An uncertain external effect resumes
the same destination identity; it must not allocate a fresh attempt.

With notifications disabled, no logical enqueue occurs and complete polling
remains available. With notifications enabled, the logical queue is mandatory;
a broken enabled path fails closed. Optional `production_notification_workers`
compose the real Core, versioned status and production queues for crash-safe
recipient expansion and delivery. They never drain quarantined readiness.
Supply `ReportingProductionSigning(resolver, algorithms, brand_json_url=...)` with the actual
resolver's RFC 9421 algorithms. Its public signing declaration and every
resolved key must agree. Production notification registration and dispatch
reject legacy Bearer/HMAC fallback; rotation keeps the advertised algorithm
contract. Private key material stays in the resolver and sender. The operator
must publish its brand document and corresponding signing keys at the declared
identity; the SDK does not assert that a supplied URL proves external ownership.
The declaration is RFC 9421 `adcp/webhook-signing/v1`, the exact supplied
algorithm set, no legacy fallback, and an 86,400-second retry horizon. Each
queue reserves the first HTTP attempt and its key/body binding before peer I/O,
using database time in PostgreSQL. Window insertion, HTTP attempt and ordinal
reservation share one transaction; a failed reservation leaves none of them.
That committed reservation anchors an immutable
deadline: attempts are permitted strictly before `started_at + 86400 seconds`,
and refused at or after that instant. A reservation with unknown HTTP effect is
still the original anchor. Retries, key rotation, crashes and restart cannot extend it; expired
deliveries are suppressed with the existing closed `lease_expired` code.
Backoff is capped at the original deadline, including after configuration changes.
An HTTP attempt reserved before that deadline may finish afterward; the limit
prevents another attempt, and does not rewrite an already observed result.
A clock earlier than the retained first-attempt timestamp fails closed.
Delivery records and protected payload bindings are retained. Drain and rebuild
the support to change algorithms or operator identity. Receivers must retain
old public verification keys and authenticated deduplication state for the
advertised interval. The tests use public verification with pinned fixture
keys; external brand/JWKS discovery and live interoperability remain separate
cross-language gates.

In the immutable `3.2.0-rc.3` capability schema, the descriptions at
`/properties/webhook_signing/properties/delivery_retry_horizon_seconds` and
`/properties/identity/description` require these declarations for applicable 3.2
agents. The JSON fields deliberately remain optional for older documents.
Unmodified schema validation therefore **accepts their omission**. The mounted
semantic assertions in `test_reporting_production_readiness.py` establish the
missing-declaration defect; `test_reporting_production_signing_schema.py`
separately preserves the original schema acceptance and normative text. The
reservation, clock, restart, signature and expiry tests establish actual
behavior. This differs from the #1179 cached-schema rejection described below.

The three workers also retain actual attempt activity through the inherited
SDK reservation/outcome transaction. `ReportingActivityProjector(worker.outbox)`
provides the corresponding authenticated account-activity read; applications
may compose that existing optional account surface. Expiry creates no fictitious
HTTP attempt or outcome. Reporting polling and immutable receipt evidence remain
the recovery path if the receiver did not acknowledge before the deadline.
Recipient fanout is separate from the finish transaction's immutable logical
enqueue. Stopping delivery may accumulate eligible pending events; it cannot
manufacture, promote or re-identify historical readiness.

## Captured status, schedule and ownership

Status keeps the canonical consumer private even when consumer feedback is off.
Revision selection examines complete history first. A unique official wins over
an unlinked retained snapshot even before the official has an artifact. Managed
needs that selected readable verified artifact. Reconciled also needs accepted
selected-official evidence and an accepted current receipt leaf for every
applicable official adjustment. Later valid artifacts or health degradation do
not erase accepted evidence or successful-materialization counts. Consumer
rejection is never a materializer retry signal.

For #1179, a generation owes only complete periods whose start is at or after
activation and strictly before deactivation. A period already begun at
deactivation remains owed in full, including its SLA. The producer and feed use
this same rule. `next_expected_at` is the nearest strictly future committed
expectation in the selected captured scope, even when current health is complete.
The immutable Draft 7 cache for `3.2.0-rc.3` rejects that otherwise-valid response
at `/allOf/2/then/not`. The effective Python SDK validator and advertised MCP
schema remove only that exact known prohibition, only for this version. The
`if` and its requirements that `scope_closed` and `coverage_complete` are both
true remain enforced, as do types, formats and all other conditionals. A
different version or changed rule is left intact. Cached files and generated
status models are preserved; this is an explicit SDK correction, not a new
upstream schema version. The executable reproduction and constraint/mounted
regressions are in `test_reporting_schedule_schema.py`; the existing SDK issue
is [#1179](https://github.com/adcontextprotocol/adcp-client-python/issues/1179).

Cross-language compatibility remains a separate blocking dependency. Every
selected compatible stable/skew client/server lane must successfully return
`complete` with the legitimate future expectation and correct semantics.
Unsupported-schema errors are acceptable only for explicitly unsupported
combinations. Python success does not establish TypeScript/protocol agreement
or release the later expert/four-quadrant gate. The coordinator owns upstream
schema/version coordination; neither suppress the expectation nor change an
otherwise-correct health value to satisfy the original prohibition.
It is absent when there is no applicable expectation. Captured timezones and
civil/DST boundaries survive later configuration changes. Producer turns keep
the 64-period maximum; durable cursors and bounded pending-acquisition queues
advance past completed windows without an unbounded history scan. Lease
acquisition takes the account lock before configuration rows and preserves
turn-primary fairness and account isolation.
Lease acquisition, release and recovery are bookkeeping, not new reporting
observations or materializer targets. The production PostgreSQL path retains
the inherited trigger and cancels only its lease-only candidate increment in
the same account-locked transaction. It never rewrites a pending attempt's
generation, epoch or external identity. Real configuration, revision and
readability changes retain their original fences.
PostgreSQL checks at most 32 admitted configuration candidates per lease turn.
A rejected source binding advances a separate durable probe rank so it cannot
permanently occupy that window. Rejection does not acquire a configuration lease
or change its frozen binding, pending acquisition, or external identity.
A held account lock instead advances a read-only sampling hint, because no
rank can be changed without that account lock. The hint belongs to one store
instance and one selected set of producer keys. Each pass examines at most 32
candidates; an empty tail may wrap once to the beginning. Only a committed
turn updates the hint. A successful lease clears it and uses the durable
turn-primary order again, so an unlocked earlier account is revisited. A new
store begins at that same durable order; it may revisit one bounded window
before continuing. Concurrent workers can repeat a sample, but the account
lock, lease predicate and durable ranks still determine actual acquisition.
Hints never create work, acquire leases or alter frozen generation identities.

Persisted PostgreSQL timestamps can omit trailing fractional zeros. Reporting
decoders accept the resulting aware precision and offset forms on Python 3.10
without changing the captured bytes or represented microsecond instant. Naive,
invalid and excess-precision values remain refused.

The shared public JSON Schema `date-time` checker has a different role: it
validates RFC 3339 wire strings without parsing them into Python timestamps.
It accepts arbitrary positive fractional widths as specified by
[RFC 3339 section 5.6](https://www.rfc-editor.org/rfc/rfc3339#section-5.6), including
the five-digit PostgreSQL form, on Python 3.10–3.13; validation preserves the exact input.
Its existing calendar, aware-offset, ASCII and seconds `00..59` requirements
remain enforced. In particular, the persisted decoder's six-digit bound and
historical seconds-bearing offsets are not imported into public validation.
This correction affects named schemas, task request/response validation and
the MCP/A2A validation paths that use them. It does not change a cached schema,
the separate #1179 conditional exception, or public-model work in #1190.
`test_schema_datetime_formats.py` checks the actual named/task schemas,
fractional precision, invalid inputs and `oneOf` selection; the configuration
mount tests check the unchanged raw timestamp through all three mounts.

Opt-in revision ownership uses page-local
`ext.adcp.reporting_revision_ownership` version 1 bindings. Every returned revision
has exactly one owner and empty opted-in pages explicitly carry empty bindings.
The full bounded buyer walk checks ownership, dependencies and counts after
all pages arrive, with defaults of 2,048 pages and 200,000 records;
all-pages-absent remains conservative legacy mode. An exact
revision's binding alone cannot prove an otherwise unknown obligation.

An in-flight B2.3 snapshot retains its original representation, ownership mode,
membership, order, counts and checkpoint after migration/activation/restart.
New snapshots may opt into the new representation. Authorization is checked on
every request: revocation can deny a continuation but cannot rebuild its history.

For #1180 the four public task/notification fields are nullable with default
`None` in canonical and bundled generated models. Missing values stay absent in
standalone and nested serialization. Readiness requires Managed, receipts require
Reconciled, and ledger/status notifications are independently opt-in. The former
global reporting serializer mask is removed; generation owns the model contract.

## Recovery and operational boundaries

Close support with `await support.aclose()` before replacing components or
migrating. Preserve pending work, immutable journals, snapshots and exact receipt
batch responses. Restart with the same admitted contracts, complete migration and
let the owned bounded workers converge. Inspect typed closed failure codes;
provider bodies and credential contexts are not persistence or diagnostic data.
An unexpected owned worker failure latches the composition unready, wakes the
shared stop signal and emits one `ERROR` record on `adcp.reporting.production`
with code `REPORTING_PRODUCTION_WORKER_STOPPED` and a closed boundary label
(`producer`, `materializer`, `projection`, `sweeper` or `notifications`). Route
that logger to the operator's alert sink. Records contain no exception text,
traceback, provider body, request identity or ambient logging context. Expected
`ReportingNotificationError` outcomes and cancellation stop the owned loops
without this unexpected-failure signal. A late in-flight error after an already
requested stop does not create a second alert or turn cancellation into an
unexpected-failure signal. Existing notification guards still
check readiness before sampling and before dispatch; already-reserved work may
finish under its existing transaction and deadline rules. Drain with `aclose()`,
repair the failed component and construct fresh support to recover; the failed
instance never silently restarts or regains its capability claim. A failing log
sink cannot prevent the stop latch or change the safe public error.
The [receipt ingress](reporting-receipt-ingress.md),
[frozen feed](reporting-frozen-feed.md) and original materializer recovery
contracts continue to apply.

Full buyer adjustment/submission automation and the `client.reporting` facade
remain later buyer work. They do not substitute for seller financial validation.
This slice remains open and unmerged pending independent exact-head review and
the separately gated downstream interoperability program.

## Seller acceptance ownership

The PR evidence index binds executed commands, counts, artifacts and tested
head/tree to every row below. An upstream implementation identity identifies
an input; it does not replace execution against the final B2.4 child.
The mounted MCP and A2A 0.3/1.0 tests use in-process ASGI. Separate real-process
and SIGKILL tests establish restart behavior; these do not establish live TCP
or the later cross-language interoperability gate.

| Requirement | Implementation owner | Current integration coverage |
| --- | --- | --- |
| Durable materializer | B2.1; B2.4 admission | Reserve/verify/finish/ACK/enabled enqueue, faults, leases, uncertain-effect identities and permanent epoch-zero quarantine. |
| Canonical authenticated consumer | B2.2; B2.4 reads | MCP/A2A trusted resolver, authorization on replay, account collisions and feedback-off/on private reads. |
| Immutable receipt transaction | B2.2 | Mixed receipt, feed, captured status and ordinal commit/rollback in both stores and notification modes. |
| Exact batch replay and mount | B2.2 | Shape preflight, semantic outcomes, original order/timestamps and concurrent/crashed final-response replay. |
| Middleware/schema boundary | B2.2; B2.4 | Actual pinned/unpinned mounts, diagnostics, #1179 narrow schema correction and #1180 nested/public models. |
| Receipt financial graph | B2.2 writes; B2.4 projection | Selected official, accepted artifact stability, rejected-leaf replacement, accepted terminality and official adjustments. |
| Frozen authorized combined feed | B2.3; B2.4 activation | Actual historical page one, installed child activation/SIGKILL, exact remaining bytes and captured schedules. |
| Feed checkpoint/closure | B2.3; B2.4 ownership | Scope-bound compact tokens, full dependency closure/counts, deterministic walk and unchanged final checkpoint. |
| Versioned status capture | B2.4 | Original captured boundaries/baselines, old-writer fence, ordered reconciliation-only and reversible health transitions. |
| Tier-correct status | B2.4; #1179 | Strict financial evidence, immutable counts/retention and committed future schedules, including complete health and DST. |
| Private scope and buyer ownership | B2.3 inputs; B2.4 wire/walk | Exact page-local ownership, malformed/mixed/conflicting walks, cross-page dependencies and conservative legacy compatibility. |
| Production tier capabilities | B2.4; #1180 | Full provider/source contracts, live component/mount checks, empty-seller discovery, polling and signing/retry truthfulness. |
| Rolling compatibility | Every slice | Eleven distinct historical inputs, isolated manifests, populated/repeated/interrupted migration, installed floor runtimes and actual restarts. |
