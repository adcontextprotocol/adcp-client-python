# Transactional reporting notification outbox

The optional reporting outbox commits a typed logical event in the ledger's
transaction, then expands subscribers and delivers HTTP in separate phases.
This implements the ledger and Managed readiness slice of [#1168](https://github.com/adcontextprotocol/adcp-client-python/issues/1168).
It retains the durable status-dirty input for the optional [C status projector](reporting-status-notifications.md).

| Committed change | Retained notification work |
| --- | --- |
| New revision or adjustment | `reporting.ledger_changed` and ordered status-dirty evidence |
| Configuration, obligation, readability, consumer statement or issue transition | Ordered status-dirty evidence |
| Managed destination/reconciliation change | Consumer-scoped status-dirty evidence |
| Verified materialization with its frozen Managed binding | `reporting.delivery_ready`, scoped to the reconciliation consumer |

This A outbox does not emit `reporting.status_changed`. The separate
[#1168C status lifecycle](reporting-status-notifications.md) provides that emitter,
clock sweeps and complete semantic fingerprinting in isolated queues. The optional
[#1168B activity layer](reporting-webhook-activity.md) adds durable HTTP reservations
and a list-accounts projection. Status and activity gates remain independent;
the A-only capability helper omits `status_notification` and sets
`supports_webhook_activity=false`.

## Optional wiring

Existing `ReportingLedgerStore` implementations and `ReportingProducer`
constructors require no new methods, callback, or argument. Concrete memory
and PostgreSQL stores accept the optional keyword `notifications=True`; the
default is off. Core producer return values and exact-content reads keep their
existing behavior. A memory outbox shares its ledger's rollback boundary but
has no process-crash durability.

```python
from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.outbox import (
    PgReportingOutbox,
    ReportingEnvelopeCipher,
    ReportingNotificationWorker,
)

ledger = PgReportingReconciliationStore(pool=pool, notifications=True)
await ledger.create_schema()
outbox = PgReportingOutbox(pool=pool)
worker = ReportingNotificationWorker(
    outbox=outbox,
    subscriptions=trusted_account_configurations,
    signing=trusted_account_keyrings,
    cipher=ReportingEnvelopeCipher(encryption_key, key_version="outbox-key-1"),
)

# At service startup, while arranging to schedule both worker phases:
fields = await worker.advertised_notifications(ledger, account_id=account_id)
# Merge fields into the producer's reporting_delivery capability block.

# Each call performs at most one leased unit of work and returns a bool.
await worker.expand_one(account_id=account_id)
await worker.deliver_one(account_id=account_id)
```

Schedule both phases for the accounts the service is authorized to process.
An empty turn is idle; use the service's normal scheduling/backoff policy.
The SDK does not start a scheduler or contact subscribers during import or
ledger construction. Supply a retained typed `ready_scope` to the capability
helper to advertise readiness. It verifies the actual configuration,
destination, and obligation binding. A profile label, capability string, or
Core subscriber request cannot create a readiness event or capability.

The helper verifies that the opted-in ledger and outbox use the same store or
pool, that current registrations have usable authentication, and that the
required objects in the PostgreSQL chain match their column, constraint, index,
trigger, and guard-function contracts. Unrelated adopter objects are allowed.
It does not infer operational readiness
from the presence of objects. Continue scheduling the worker while advertising
these fields. Custom stores can implement the additive outbox protocol;
automatic capability verification conservatively covers the SDK reference stores.

## Domain commit and replay

Every domain mutation and its enqueue use the exact same connection inside
an explicit `async with connection.transaction()`, including autocommit
pools. An enqueue failure rolls back the mutation. Committed logical events
are immutable and survive producer restarts. The causal key contains account,
consumer namespace, closed cause kind, non-null cause ID, and cause generation.
Readiness identifies the full `(account_id, consumer_id, materialization_id)`.
An identical mutation replay creates no event; a conflicting identity cannot
commit a ghost event.

Each event owns one notification ID and `fired_at`. Every subscriber shares
that ID. Each subscriber/emission generation gets a cryptographically random
idempotency key and its own prepared body. Re-emitting an event with
`outbox.reemit(...)` advances the emission generation and generates new keys;
ordinary retry changes neither generation, key, nor body bytes.

A configuration generation's *content* stays immutable, while the rc.3
lifecycle state it carries -- activation, deactivation, and the recovery and
retention windows -- keeps evolving on that same generation
(`reporting-delivery-config-state.json` walks one generation from `ready` to
`inactive`). A re-put that changes only those fields therefore applies to the
retained generation and co-commits one status-dirty generation; an unchanged
re-put stays a no-op and enqueues nothing; changed content is still a
`CONFIGURATION_GENERATION_IMMUTABLE` conflict that writes nothing. The
reconciliation reference guard exempts exactly this lifecycle state, so a
Managed generation bound by reconciliation records can still be deactivated.

A dirty record follows the retained evidence rather than a predicted
transition: an idempotent re-acknowledge of an issue changes nothing and
enqueues nothing, while a repeated waive does move `retired_at` and stays
reconstructable. Existing issue return values are unchanged by opting in.

Status-dirty records form an account-ordered journal, with a trusted optional
generation/obligation/consumer/feed scope, cause generations, and immutable
record references. Readability, configuration lifecycle, and issue lifecycle
retain before-and-after evidence, so rapid reversals remain replayable.
Consumer statements and reconciliation records remain readable in their
original consumer namespace. Checkpoints use account/projector compare-and-set.
They do not delete evidence or project a wire status. The clock-dirty method
is a handoff seam, with no automatic clock sweep in this slice.

The concrete issue methods accept optional `status_scope=ReportingStatusScope(...)`.
The store validates supplied references in the issue's account and consumer
namespace. It never parses `issue_key`. Without a typed scope, a dirty record
invalidates the account and its specified consumer broadly. An occurrence's
typed scope cannot be retargeted during its lifecycle.

## Subscriber configuration and transport

`ReportingSubscriptionResolver` must resolve from trusted account registration
state. A `ReportingNotificationSubscription` combines account, principal,
subscriber, normalized URL, exact event types, configuration revision,
authorization and proof-of-control references, active/authorized/proof-valid
flags, and one exclusive authentication mode. The registrar must actually
verify principal authorization and the account/subscriber/URL proof of control;
the references are evidence pointers, not substitutes for those checks.
Never construct registrations from an unauthenticated request or notification.

Fanout uses **active-at-expansion semantics**. It resolves one complete snapshot,
validates uniqueness and fingerprints, then inserts all matching deliveries and
the completed-expansion checkpoint in one transaction. A valid empty snapshot
completes. A transient resolution error retries. A crash during insertion rolls
back every subscriber row; a restart can resolve a new snapshot without mixing
membership. An external resolver is not transactionally atomic with the ledger.

AES-256-GCM encrypts the complete routing envelope, including the full query
URL, credential mode/material, typed event and exact prepared body. Canonical
AAD binds every routing column: account, consumer, subscriber/principal,
delivery/event/notification/idempotency IDs, cause and emission generations,
URL digest, configuration fingerprint, auth mode/signing scope, body digest,
envelope version and encryption-key version. Decryption/authentication runs
before configuration resolution, signing, DNS, or HTTP. Old encryption keys
must remain available while retained deliveries can use them; `previous_keys`
supports rotation without rewriting an immutable delivery.

Every attempt resolves the exact account/subscriber/event configuration again,
including authorization, proof validity and the full credential fingerprint.
Removed, deactivated, or replaced registrations are terminally suppressed.
Bodies are never retargeted. RFC 9421 attempts resolve current trusted signing
material by account/principal/signing scope, producing fresh signatures during
key rotation. Legacy bearer and HMAC modes are exclusive with RFC 9421.
The worker constructs its own concrete hardened sender; injected sender
objects/subclasses, arbitrary HTTP clients and URL rewrite hooks are not accepted.

Prepared bodies use deterministic canonical UTF-8 and the exact bundled
`3.2.0-rc.3` named schema validator, including the conditionals omitted by the
generated models. The mapping is an allowlist with recursive secret rejection;
it copies no reporting rows, resource/object lists, signed or activation URLs,
credentials, tokens, extensions, or adopter metadata.

The SDK-owned transport permits public HTTPS on port 443, validates all DNS
answers, pins the accepted IP, verifies TLS against the original host, ignores
proxy environment settings, and refuses redirects. Rebinding to loopback,
private, link-local, metadata or IPv6 ULA addresses is rejected.
Outbox state retains only closed local error classifications. HTTP library logs
are suppressed within this transport context so URL queries, auth headers,
signatures and provider responses cannot spill into logs; unrelated traffic
retains its logging behavior.

## Leases, retries and retention

PostgreSQL claims use `FOR UPDATE SKIP LOCKED` in short transactions with
unguessable tokens and database-clock expiration. ACK, retry release,
suppression and quarantine are fenced by account, consumer namespace, token
and expiry. The prepared-request seam rechecks the lease after DNS/signing,
immediately before HTTP. HTTP runs outside database transactions. A crashed claim does not
consume an HTTP retry budget. Network errors, 408/425/429/5xx, and transient
signing/DNS failures retry; permanent 4xx, invalid payload/routing evidence,
permanent scopes and poison rows quarantine without blocking other subscribers.

HTTP acceptance and worker ACK cannot be a single transaction. Receivers must
deduplicate using a trusted publisher identity that survives key rotation and
the idempotency key. A lost ACK can produce another authenticated request with
identical body/key and a fresh signature. Polling remains the recovery path.

The base outbox retains events, expansion checkpoints, prepared bindings and dirty
evidence indefinitely. The optional activity layer retains pending reservations
and counters indefinitely; its scoped purge enforces a 30-day terminal-history floor.
Do not delete parent events, keys, or prepared bindings while any delivery is
nonterminal or within an adopter's promised retry/activity retention horizon.

## Migration and conformance

`reporting_notification_outbox.sql` follows the reviewed four-file foundation
chain. It adds notification events, expansion/delivery leases, ordered dirty
records, projector checkpoints, and typed issue scope storage. It rewrites and
backfills no ledger evidence. `reporting_webhook_activity.sql` follows as an
additive sixth step. `create_schema()` installs all six steps atomically;
opted-in stores and `PgReportingOutbox.create_schema()` also validate the complete
required outbox contract before committing. Activity startup additionally validates
the sixth step. Readiness validates required objects independently and ignores
unrelated adopter additions. Concurrent and repeated installations serialize on the schema
advisory lock. The standalone outbox SQL is atomic even on an autocommit
connection with the foundation already installed. See
[reporting ledger migrations](reporting-ledger-migration.md).

The private shared harness is `tests/conformance/reporting/_reliable_support.py`.
It injects `ManualClock`, scripted sync/async adapters, barriers, failure plans,
bounded drains and deterministic destination/receiver stores into the same
memory/PostgreSQL vectors. Essential cases are not marked `integration`.

`test_reporting_notification_process_matrix.py` runs separately pooled producer,
fanout, HTTP worker, receiver and observer services. Named IPC and SQL barriers
cover commit/fanout and real TLS HTTP acceptance/ACK. All child, pipe, receiver
and barrier waits have hard watchdogs with sanitized role/PID/checkpoint
diagnostics; no timing sleeps control an interleaving. The receiver fixture
self-check verifies both rotation keys before the full process lane is run.
The distribution tests build an sdist and its wheel, install each with PostgreSQL
absent, then install each with `[pg]` and exercise migration, commit, retry,
activity projection and restart on PostgreSQL 16.
