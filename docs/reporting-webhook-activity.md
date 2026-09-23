# Durable reporting webhook activity (#1168B)

This optional layer extends the [reporting outbox](reporting-notification-outbox.md).
It records reporting HTTP attempts and projects them into visible
`list_accounts.accounts[].webhook_activity`. It adds no methods to the legacy
`AccountStore` or `ReportingLedgerStore` protocols. The generic webhook supervisor
does not provide this reporting activity contract. Status notification projection
and its clock sweeper remain #1168C.

## Mounting and lifecycle

Install `adcp[pg]` and use PostgreSQL 16. The
[fully typed example](../examples/reporting_webhook_activity.py) shows trusted
identity resolution, registration persistence, migration, capability validation,
both worker phases, HTTP serving, bounded draining, and pool shutdown.

Construct the opted-in `PgReportingLedgerStore` (or reconciliation store),
`PgReportingOutbox`, and `ReportingNotificationWorker` on the same pool. Pass that
exact outbox instance as the worker's `activity=` argument. Construct
`ReportingActivityProjector(outbox)` and
`ReportingActivitySupport(worker, ledger, projector)`. Mount the support object as
`reporting_activity=` and the projector as `account_activity=` on
`create_adcp_server_from_platform` or `serve`.

Run `ledger.create_schema()` before serving. In an async startup hook use
`validate_at_init=False`, then await
`validate_capabilities_response_shape_async(handler)` on the pool's event loop.
Schedule expansion and delivery for the service's authorized accounts and keep
the worker healthy while advertising activity. Stop accepting requests and new
claims before draining in-flight work. If a drain deadline requires cancellation,
retain the pending row and parent lease; a later worker reclaims the delivery.
Close the pool after the workers exit. Encryption keys and old key versions must
remain available while their immutable prepared deliveries can retry.

The capability helper checks required database objects and the concrete mounted
chain. `support.capability_flags(account_activity=projector)` returns independent
`reporting` and `account_notifications` booleans to use in an adopter's existing
capability declarations. It does not create account lifecycle emission support.
The handler checks truthy declarations after static and request-specific
projection, without mutating a shared capability model. Unsupported truthy
overrides fail startup/discovery with a constant diagnostic.

| Mounted components | Reporting activity | Account activity capability |
| --- | --- | --- |
| None, outbox only, or memory only | false | false |
| Durable writer without activity projector | false | false |
| Durable outbox, attempt writer, worker, projector | true | false |
| Above plus list-accounts projector and account listing | true | true |

Relationship notification support is independent and supplies no evidence for
either flag. The memory implementation supports shared conformance vectors; it
cannot justify a durable claim.

## Canonical consumer and account visibility

Call `resolve_reporting_consumer(auth_info=..., agent=...)` when registering a
trusted subscription and persist its result as `principal_id` with the authorized
configuration. The same resolver runs for requested activity reads. It checks
every present identity: `ResolveContext.agent.agent_url`, `AuthInfo.principal`,
`AuthInfo.agent_url`, and `HttpSigCredential.agent_url`. A sole valid registry
identity (including typed API/OAuth resolution) or a sole valid auth identity is
sufficient. All present values must agree exactly. Blank, nonprintable, or
reserved anonymous values fail, including case variants of `anonymous`, `anon`,
`unauthenticated`, `none`, and `null`. Normalize legitimate aliases in trusted
authentication adapters before this boundary.

Neither an account assertion, request body, subscriber ID, nor
`consumer_namespace` chooses this principal. The async worker restores it from
the trusted persisted subscription and revalidates its principal and entire
configuration fingerprint before preparation. Keep subscription URL/credentials
in access-controlled configuration storage; the outbox protects its routing
snapshot with authenticated encryption.

The list-accounts handler passes the original optional `account`, `status`,
`sandbox`, and pagination filters to the legacy store. Activity-only request
fields are not store arguments. Enrichment runs after existing response
projection, exclusively for account IDs the store returned as visible. Every
activity SQL read/write includes both account and principal predicates. Limits
are per account, integer 1–200 (default 50), applied after those predicates.

Unrequested or unsupported activity is omitted, including adopter-supplied
activity fields. Supported, requested activity replaces them with canonical rows
or `[]`. Principal resolution is required only for supported, requested activity;
anonymous legacy list calls retain their existing behavior. List, dict, and
Pydantic store results retain their envelope, errors, context, pagination, and
final credential scrub.

## Reservation boundary and outcomes

`WebhookSender.before_attempt` is single-use. DNS/SSRF validation and signing
finish first, followed by the final lease fence and activity reservation,
immediately before HTTP. A preflight DNS/SSRF failure creates **no activity row**.
DNS or signing can outlive a lease; an expired final fence prevents reservation
and HTTP. A reservation database failure prevents HTTP as well.

The reservation transaction locks the exact parent by account, canonical
principal, consumer namespace, delivery ID, lease token, and database expiry.
It then locks/increments the retained head with an atomic upsert/returning path.
The lock order is always parent then head. The head is keyed by account,
principal, subscriber, and idempotency key; its 1-based counter never uses queue
claim counts and cannot be deleted or reset. Notification/parent references,
identity, request diagnostics, tokens, and attempt number are immutable.

| Observed result after reservation | Activity status | Parent policy |
| --- | --- | --- |
| HTTP 2xx | `success` | complete |
| Other HTTP response | `failed` | retry 408/425/429/5xx; terminal otherwise |
| HTTP timeout exception | `timeout` | retry |
| Connect, TLS, socket failure | `connection_error` | retry |
| Crash, cancellation, unknown completion | `pending` | expire/reclaim |

Activity terminalization precedes the parent ACK. It uses the primary identity,
opaque reservation token, and pending-state predicate, without requiring the
parent lease to remain current. A late known response can complete only its own
attempt. Failed or uncertain terminalization prevents the parent ACK. Queue
quarantine, circuit state, provider response bodies, and exception prose are not
activity outcomes.

The reservation-to-peer-I/O and post-HTTP/pre-activity-ACK windows cannot be made
atomic. A crash in either window leaves `pending`: it cannot establish whether
the peer observed a request. Reclaim creates a new attempt with the exact same
notification, body bytes, and idempotency key. Worker deadline cancellation is
also uncertain; it is not an HTTP timeout result. Receivers still deduplicate by
authenticated sender and idempotency key.

## Projection, redaction, and retention

Rows sort by `fired_at DESC`, then C-collated notification ID, idempotency key,
subscriber ID, attempt, and delivery ID descending. Equal timestamps have stable
per-account ordering. PostgreSQL supplies fired/completed timestamps; elapsed
HTTP response time uses a monotonic clock. Optional sequence/notification IDs
are omitted when absent. The completion, HTTP code, latency, and error fields
retain explicit nulls required by the state contract. Each projected row is
validated against bundled `core/webhook-activity-record.json` (3.2.0-rc.3).

Displayed URLs remove userinfo, query, and fragment. Path segments are decoded
for detection; UUID, JWT, long hex/base64, encoded credentials, and other unknown
segments become the constant `redacted`. This deliberately conservative policy
also hides unfamiliar non-secret route words; a small public-route allowlist
preserves useful structure. The sanitized URL is validated as `AnyUrl`. Raw
subscription URLs are bounded at 8,192 characters and never interpolated into
sanitizer exceptions. Activity plaintext columns contain only the sanitized URL
and closed diagnostics; transport logs cannot disclose URLs, headers, secrets,
or provider errors.

Use `purge_activity(account_id=..., consumer_id=..., now=...)` for each authorized
account/principal. PostgreSQL ignores the caller's clock for retention and uses
database time. Only terminal rows strictly older than 30 days by `completed_at`
are removed; exactly-on-boundary and pending rows remain. `retention_days` may
increase that floor, never reduce it. Heads are retained even when every terminal
row is purged. Pending orphans remain readable after parent loss. No status clock
sweeper or broad unscoped tenant purge is installed by this slice.

## Migration and rolling upgrades

The ordered packaged chain is `reporting_ledger.sql`,
`reporting_ledger_account_generations.sql`, `reporting_ledger_obligation_currency.sql`,
`reporting_ledger_reconciliation.sql`, `reporting_notification_outbox.sql` (#1168A),
then `reporting_webhook_activity.sql` (#1168B). `create_schema()` executes the chain
in one transaction. The B step is also atomic on its own, serializes concurrent
installations, backfills nothing, and adds only B tables/indexes/constraints/
functions/triggers. It does not change any A table or attach a new object to one.

The actual-binary rolling controls pin integrated A commit `17ee407a`, including
its checkpoint-schema and database-locale readiness corrections. That A binary
can continue existing work on a B-upgraded database without rejecting B's
additive objects or claiming activity. These controls do not qualify the earlier
`21bf443e` snapshot, whose schema fingerprints depend on database collation.
B accepts
the A required-object subset for existing outbox work, but its activity readiness
fails closed until B migration commits. Attempts against an A-only schema fail
without HTTP or parent ACK; they become retryable after migration.

**Activation ordering:** migrate the additive schema while A still serves its
existing configurations, then drain/replace A workers before enabling B activity
and new canonical URL-principal subscriptions. A's registration validator rejects
URL principals, and A workers do not record activity. Mixed A/B workers therefore
cannot justify advertising complete activity coverage. This is an activation
barrier, not an A-table rewrite or data backfill.

B validates a required-object subset rather than hashing each whole table.
Required columns, named constraints, ready/valid indexes, enabled triggers and
their definitions, and guard function definitions/security/volatility must match.
This includes A's restatement checkpoint table, columns, constraints and indexes.
Unrelated adopter columns/indexes/constraints/triggers are ignored. Diagnostics
are `notification_schema_unready:{missing|disabled|changed}:<required object>` or
`catalog_unavailable`; they contain bundled names, never catalog/provider prose.
Install a missing migration, re-enable a disabled required object, or restore a
changed required definition from the matching release before advertising. Do not
regenerate the bundled manifest from a damaged adopter database to bypass checks.

The manifest regression compares all 453 required objects across two fresh random
schemas, repeats each unchanged migration with zero manifest diff, and compares
installed wheel/sdist migrations to the same manifest. Schema names and OIDs do
not identify required objects. Function source, security mode, volatility,
strictness, parallel safety, leakproof flag, and function-local settings (including
`search_path`) remain in the fingerprint; changing them fails readiness.
