# Durable reporting status notifications (#1168C)

Install `adcp[pg]` and mount the optional status lifecycle to publish
`reporting.status_changed`. `get_reporting_status` remains authoritative. C
supports Core status; Managed/Reconciled materialization and receipt expiry
clocks remain in D. An account with destination bindings cannot advertise C
status notifications. The existing ledger, revision outbox and activity APIs
remain available independently.

## Mounting and capability truth

The wire declaration is
`media_buy.reporting_delivery.status_notification: "reporting.status_changed"`,
paired with `status_task: "get_reporting_status"`. The reporting feature gate and
RFC 9421 webhook signing declaration are also required by the bundled rc.3
schemas. C does not infer reporting support from relationship notifications.

`ReportingStatusSupport` is an explicit composition of the concrete PostgreSQL
status store, pure projector, due sweeper, C outbox/HTTP worker, signing and
subscription resolvers, and a mounted `ReportingStatusNotificationHandler`.
`scheduled=True` is the adopter's commitment to supervise all worker phases;
the SDK does not start them implicitly. Capability checks validate schema,
baseline, policy agreement, the actual `get_reporting_status` tool, and signing
material for validated `readiness_subscriptions`. An empty subscription lookup
does not prove signing readiness. Memory implementations are conformance tools
and never advertise durability.

Activity storage is optional for status delivery. If the C HTTP worker has an
activity recorder attached, readiness also validates that recorder's C tables
and guards because it runs before HTTP. With `activity=None`, status delivery
requires no activity storage. C logging never depends on B activity storage;
the combined activity capability independently validates both histories.

The capability request has **no account selector**. Declare `account_ids` as the
complete deployment or authenticated-principal account surface and explicitly
set `account_surface_complete=True`. Every listed account must be migrated and
baselined. One unbaselined visible account suppresses the global claim. Request
body, `context`, `ext` and a convenient single-account sample cannot select
readiness. Update that complete surface before exposing a new account.

Readiness is structural. A valid waiver, an outage, incomplete coverage and
other current business health never disable a ready feature. Explicit claims
with missing components raise secret-free configuration errors; omission stays
omission. Calling `support.advertised_notifications()` explicitly opts into
automatic advertisement when ready. Generated reporting capability models emit
only explicitly set optional task, notification and tier fields. Required
`configuration_task` and `status_task` declarations must also be supplied by the
adopter; do not rely on generated defaults to mount handlers. Other claimed
tasks must have real overrides and appear in `tools/list` and MCP/A2A dispatch.

`DecisioningPlatform` does not gain reporting task implementations from a
capability declaration. Compose real handlers, as in
[`examples/reporting_status_notifications.py`](../examples/reporting_status_notifications.py),
or leave status notification support unadvertised. The example delegates
account configuration and immutable revision-content reads to actual adopter
handlers and implements consumer-status ingest on the shared transaction
participant. It checks types under `mypy --strict`.

## Registration, authorization and activity

Account notification configs register `event_types: ["reporting.status_changed"]`.
A reporting-only `NotificationConfig` omits the generated
`product_payload_view=legacy` default. Global agent configs require explicit
`all_authorized_accounts:true`; the trusted registry adapter expands only
authorized accounts into normalized `ReportingNotificationSubscription` rows.
Both expansion and every HTTP attempt re-resolve current authorization, endpoint
proof, registration revision and signing scope. Private events match the
canonical consumer namespace; seller events broadcast to authorized account
subscribers. The private namespace is not included in the webhook body.

Resolve identity from verified middleware/registry inputs with
`resolve_reporting_consumer`. All present principal, registry and signed-agent
coordinates must agree. Canonical BuyerAgent HTTPS URLs and public opaque
principals are admitted. Raw and repeatedly percent-decoded JWT/JWE, AWS key,
credential assignment, query parameter, Basic authorization and recognizable
provider-token shapes are rejected before storage. Errors contain no rejected
identity. These checks reinforce a trusted-authentication boundary; they cannot
establish whether arbitrary opaque text is a credential.

The SDK account wire models currently do not echo reporting/notification
configuration through `sync_accounts`. This slice supplies the durable emitter
and a trusted registry seam, not a completed registration/echo implementation.
Adopters own that registry, account authorization, proof verification and
configuration revisioning.

Activity is independent of status. The exact B-only `ReportingActivitySupport`
path is preserved. To expose both queues, use the closed concrete
`PgReportingActivityUnionStore(b_outbox, status_outbox)` and give that same
object to `ReportingActivityProjector`. The support object proves that both
concrete workers share the expected pool, cipher and resolvers and that the C
queue is scheduled. It does not require C baseline/projector/sweeper readiness.
The union performs one global ordering and limit, with queue rank as its final
tie break; it never applies a per-table limit first. Purge removes terminal
attempts in both tables and retains pending attempts and monotonic attempt heads.
Account-notification activity additionally requires the mounted account listing
projection. Reporting activity and account-notification activity remain separate
wire flags.

## Migrate, baseline and run

`ReportingStatusNotificationLifecycle` is optional. Existing custom
`ReportingLedgerStore` and `ReportingNotificationOutbox` implementations retain
their structural contracts. Pure types and memory implementations import without
PostgreSQL. PostgreSQL exports load lazily and construction without the extra
raises an actionable `adcp[pg]` installation hint.

`PgStatusNotificationStore.create_schema()` executes the six existing ledger/A/B
steps plus `reporting_status_notifications.sql` and
`reporting_status_selector_version.sql` in one serialized transaction.
External migration tools must execute that same chain atomically. The original
`required_schema.json`, A/B objects, functions and constraints are unchanged.
`required_status_schema.json` independently validates C status and C activity
objects. Missing C DDL suppresses status without changing B readiness.

## Selector epoch cutover (#1167B1)

B1 changes whole-history revision selection and directional feed issue scopes.
Install the additive checkpoint migration, then **stop and drain old C
projectors and sweepers before scheduling v2 turns**. Core/source, A/B outbox and
C HTTP/activity writers remain usable. Installing schema alone does not fence
old C; account cutover is explicit, persisted and separate from its completion.

Drive `ReportingStatusProjector.rebuild_once()` (or
`ReportingStatusService.rebuild_selector_once()`) until idle. This uses indexed
discovery of every populated stale account compatible with the store's original
escalation policy; no adopter account list or full periodic scan is needed.
The existing service's `drain()` includes these turns. Use the same escalation
policy as the original baseline; an explicitly targeted mismatched policy fails
closed. Ordinary `project_one(account_id=...)` also resumes that account's
interrupted cutover.

Each first turn takes the existing account advisory lock, locks its checkpoints
and commits a checkpoint-local v2 writer floor plus an account transitioning
policy. A separate `selector_semantics_version` remains stale until projection
has committed. The new guard examines only the checkpoint and a
transaction-local v2 marker, avoiding an account-row lookup from old row-only
due claims. Old pending lease identities are retained, but old claims,
completions, projectors and readiness fail closed after the fence. Migration
does not wait for those leases to expire. Transaction-local markers are cleared
when connections return to the pool. Old named guards and their manifests are
unchanged, so old `create_schema()` cannot remove the independent v2 guard.

Subsequent short transactions drain captured source boundaries in `through`
order, replay overdue semantic deadlines chronologically, then project current
state and mark the account complete atomically. Checkpoint/event failure rolls
back the entire turn. The scope union includes all retained checkpoints,
including those no longer returned by current scope discovery; absent source
history has a stable `HISTORY_UNAVAILABLE` result and no obsolete due deadline.

No baseline, scope key, event, queue or activity history is deleted/reset. The
six-column scope identity, generation, baseline highwater, dirty cursor and
leases retain their meaning. Selector epoch is non-key metadata; canonical
fingerprints retain `version: 1`. Unchanged canonical health/issues only advance
the selector epoch and emit no event. Changed topology or issue membership
emits the corrected status with its existing previous health and next monotone
generation. Newly baselined accounts start at v2 without migration events.

Readiness requires current schema, baseline, target epoch and zero stale or
incomplete checkpoint migrations. Account transitions are isolated. The memory
reference imports old shared-state images as v1 and performs the same restartable
transition. These objects and `required_status_selector_schema.json` belong only
to C checkpoint semantics; B1 adds no materializer persistence. See
[B1 destination I/O](reporting-destination-writer.md) for the B2 rollout dependency.

Default-off Core lifecycle writes also work before the A outbox migration. That
schema has no issue-scope table: reads derive scope from retained status evidence
until migration makes scope persistence available. Notification-enabled writers
require the scope table and fail closed when it is missing. Migration preserves
the retained lifecycle rows; baseline repairs scopes before advertising status.

Run deterministic turns in this order:

1. `service.migrate()`.
2. `service.baseline()` for every account in the advertised surface.
3. `service.ready()` and capability validation before accepting traffic.
4. `service.project_dirty_once(account_id=...)`.
5. `service.sweep_due_once(account_id=...)`.
6. `service.expand_once(account_id=...)`.
7. `service.deliver_once(account_id=...)`.
8. Stop scheduling, await in-flight turns, `service.drain()`,
   `service.aclose()`, then close the caller-owned pool.

`drain()` processes work due now with a bounded turn limit. Future retries and
deadlines remain durable. HTTP is always outside checkpoint/source transactions.
The example supervises worker and HTTP lifetimes together, stops serving on
worker failure and waits for in-flight work before closing the pool.

Baseline acquires the same account advisory lock as source mutations, records
all seller and extant private configuration/obligation snapshots and deadlines,
and advances the account dirty high-water in one transaction. It emits nothing.
Interruption rolls back scopes and high-water together. A mutation before the
lock belongs in the baseline; one after it is projected as a new boundary.
Scopes born after baseline fire an initial publishable event with absent
`previous_health`, including an immediately action-required zero-window scope.
The first later transition of a baselined scope uses its stored baseline health
as `previous_health`.

## Source boundaries and issue projection

One source boundary is **one committed source transaction**, even when it writes
several dirty rows or affects several scopes. Use `ledger.transaction()` to group
related source operations. An additive `AFTER INSERT` trigger on A's dirty journal
groups live old-writer rows by PostgreSQL transaction identity. A deferred trigger
captures the complete final projection input at commit, with database `as_of`.
Explicit injected clocks are for conformance/replay only. Do not force deferred
constraints early and then keep mutating reporting evidence in the transaction:
the guard rejects writes after a boundary was captured.

The account cursor advances only after the whole boundary fanout succeeds. Typed
scope checkpoints use separate C-collated, non-null columns:
`(account_id, consumer_namespace, delivery_config_id, version, scope_kind,
obligation_namespace)`. Configuration and obligation identities are never
concatenated. An obligation change projects its own scope and configuration
aggregate. Seller changes expand into every extant affected private consumer
scope. Broad legacy issues conservatively expand across known configurations
without parsing opaque issue keys. Replaying a crash cannot lose half the fanout.

Captured input preserves committed readability and mismatch cycles even when
the projector has not drained: true/false/true and open/resolve/recur do not
collapse into a read of final evidence. The journal and immutable boundary input
are retained; C does not introduce a boundary-history retention policy.

`project_status_scope(StatusProjectionInput(...))` is backend-free and shared by
the handler and projector. It receives one database `as_of`, selected scope,
configuration/obligation/revision evidence, this consumer's status chains and
persisted issue lifecycle/scope. It returns health, full public issues, pending
count, next deadline, and closed lifecycle intents. A result is publishable only
after intents have converged. The projector applies intents, rereads and projects
again on its held connection. PostgreSQL consumer-status ingest records the
statement and its lifecycle/refinement in one source transaction. Upgrade repair
uses the earliest provable observation, not projector wall time. Lifecycle writes
inside projection do not create self-dirty feedback.

Lock order is account advisory lock, account boundary cursor, typed checkpoints
in canonical order, then evidence/status/lifecycle rows. Connection-bound readers
and helpers avoid acquiring another pooled connection under those locks; the
pool-of-one test instruments backend IDs and query order. Generic custom-store
handler loading can continue using public store methods without claiming this
durable lifecycle.

Scope refinement only fills missing generation, obligation and feed coordinates
after checking ownership. It never changes a known coordinate or consumer.
`obligation_missing` starts as a private configuration issue using ingest
`opened_at`, and attaches when the seller obligation appears. An agreeing status
retires an open mismatch. AdCP 3.2.0-rc.6 permits a waiver only after explicit
bilateral agreement for the exact caller/account issue, causing statement and
diagnosed conflict. The adopter must obtain that agreement and retain its private
consent audit before calling `set_issue_state(state="waived")`; `external_ref`
remains inert correlation text and is never accepted as proof of consent.

The store captures the immutable statement ID and a private conflict fingerprint
under the same transaction as the waiver. PostgreSQL retains this binding in a
separate additive table, preserving older workers' lifecycle-column fingerprints.
That exact waiver removes the public
issue and restores underlying seller health while retaining the consumer
statement. Advertised recovery notifications follow the readable projection and
carry no issue IDs when the last impairment clears. Other impairments remain.
A later statement or different diagnosed conflict gets a new issue, even if the
clock has not advanced. The terminal waiver is never overwritten as `resolved`.
Its successor has a separate private condition key so existing uniqueness guards
and the original audit row remain intact. Operators use the selected occurrence's
private key; they must not reconstruct it from a period or reuse an old waiver key.
Historical waivers without an exact binding are retained but cannot silently
suppress a newly projected disagreement. No consumer evidence is rewritten.

Every delayed/action-required projection includes an authoritative published
issue of matching severity. `REPORTING_COVERAGE_INCOMPLETE` represents partial,
none or unknown coverage; `HISTORY_UNAVAILABLE` represents requested history
older than retention. Both are returned by `get_reporting_status`. Healthy,
waiting and complete scopes have no published issues. Readability considers the
selected current required leaf, so an older readable snapshot cannot hide an
unreadable current restatement.

The fingerprint is JCS over version, typed scope, health, exact period/feed/media
selection and every field of every public issue, sorted by ID/canonical bytes.
Duplicate IDs with divergent semantics are rejected. Only after fingerprinting
the full set are the first 16 unique sorted IDs selected for the wire. Issue-only
changes, including item 17 or a changed recommended action, advance generation
with `previous_health == health`. Each logical generation allocates a random
notification ID after locking its scope; `fired_at` uses database time. Re-emission
preserves notification identity. HTTP retries preserve body and idempotency key.
All queue, cipher/AAD, re-emission, delivery and attempt identities carry the
canonical consumer namespace.

Filtered reads bind exact period, feed and media selection into their cursor.
Incremental continuations also retain their `changes_after` lower bound: callers
may omit it when following the cursor, but an explicitly different bound is
rejected. Older cursors without that binding require a fresh walk. As before,
an intervening account write can invalidate a cursor; the reader rejects mixed
ledger boundaries rather than promising progress under continuous writes.
Custom stores without the optional status participant replay retained immutable
records from their real `open_snapshot`/`read_page` boundary when it advances.
Consumers deduplicate that permitted replay by record identity; no synthetic
per-record sequence or durable notification readiness is inferred for the fallback.
Media-filtered responses include `scope.media_buy_ids`. Absent `next_expected_at`,
`previous_health` and `issue_ids` are omitted, never JSON null. An empty closed
selected horizon is complete, with full vacuous coverage, empty sets, no next
expected time and `evaluated_at` equal to captured ledger time. Mixed-generation
retained coverage starts at the latest per-generation retained/activation bound.

## Database-clock deadlines and fencing

Indexed `next_due_at` covers expected time, frozen recovery, readable period-end
completion, stale-received grace and mismatch escalation. Equality transitions
use `>=`. Expected equal to recovery yields one waiting-to-action-required event;
it never fabricates delayed. Satisfied current evidence wins coincident deadlines.
The earliest grace/escalation threshold can change health, and a later escalation
can still change public recommended action at the same health.

Claim uses database `clock_timestamp()`, `FOR UPDATE SKIP LOCKED`, random tokens
and expiring leases. Exact expiry reclaims with `<=`; ACK/release require `>`.
The final transaction re-locks, revalidates current source evidence and atomically
commits lifecycle, checkpoint, event, account cursor and lease ACK. An expired
final ACK rolls the whole turn back. Every mutation and no-op/stale sweep repairs
or clears `next_due_at`, so an invalidated deadline is idle on the second turn.

Pending committed source boundaries take priority over a claimed deadline: replay
them first and revalidate current evidence so a revision/readability/issue/receipt
race cannot stale-fire. When there is no pending mutation, a late sweep walks all
remaining semantic deadlines in order. Deadlines that do not change canonical
public semantics are discarded; pending-count changes alone are not events.

## Rolling compatibility and gates

C uses separate **events, expansions, deliveries, attempts and attempt heads**.
It does not extend A/B event/cause constraints, reuse B delivery parents or replace
`reporting_webhook_attempt_guard`. Actual old A/B workers can continue draining
A/B work while pending C queue rows remain byte/state-identical. Repeated old
`create_schema()` leaves C catalog and data intact. Old B writes are captured as
grouped C source boundaries and C restart converges once.

The actual-binary controls pin integrated A `17ee407a` and integrated B
`0f34c666`. A includes the checkpoint-schema inventory and explicit byte ordering
for aggregate catalog fingerprints; B uses the per-object required manifest.
No compatibility artifact is patched by the tests. Rolling compatibility with
pre-`17ee407a` A and pre-`0f34c666` B snapshots is untested and unclaimed here.
In particular, the earlier `21bf443e` A snapshot has locale-dependent fingerprints;
these controls do not qualify it by forcing the database locale to C.

There is a separate readiness limit: integrated A hashes the entire dirty
trigger set, so C's additive boundary trigger makes **A notification startup
readiness fail closed**. Default-off/core A writes and already-running A workers
remain compatible with the isolated queues. Integrated B has subset validation
and remains notification/activity-ready. This release does not claim that an
unmodified A worker can restart with notification readiness green on C. Deploy B
or C for restarts that need that readiness check.

The rolling jobs use the database service's ordinary locale. SDK identity columns
remain `TEXT COLLATE "C"`; that column-level identity rule does not require a
C-collated database. B and C fingerprints remain per-object, and A's integrated
inspector pins aggregate ordering explicitly. The optional test precondition
checks only for a PostgreSQL test environment and drivers, without restricting
the locale or weakening any readiness comparison.

Every writer in the advertised status account surface must enable the durable
dirty journal. Compatibility of default-off core writes does not make those
unjournaled writes eligible for status notification projection.

The conformance suite shares memory/PostgreSQL projection vectors and the
`ManualClock`, `Barrier`, `FailurePlan`, `ReliableHarness`, `NotificationHarness`
and `ServiceProcess` infrastructure. Separate processes omit ManualClock for
database-clock deadline/crash roles. Two independently pooled sweepers compete at
each deadline; SQL-captured time proves production `>=`, reclaim `<=` and ACK `>`
without timing sleeps. Named crashes cover due claim, lifecycle/pre-event,
event/pre-commit, checkpoint+event commit/pre-ACK, consumer pre-lifecycle and
baseline scopes/pre-high-water. Actual detached A/B artifacts verify module
origins and run concurrently with C work. A bounded PostgreSQL status CI job
keeps this process matrix separate from the existing reporting job.
