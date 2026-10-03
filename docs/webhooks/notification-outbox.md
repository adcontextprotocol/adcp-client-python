# Transactional notification delivery with PostgreSQL

`PgNotificationOutbox` is a separate opt-in table and worker for non-task
notifications. Install `adcp[pg]` and prepare the notification using the
[preparation API](prepared-notifications.md). Existing task and reporting outbox
contracts and migrations are independent.

```python
from adcp import PgNotificationOutbox, WebhookSender

# Use the same pool and connection as the business operation. Encryption key
# material comes from your secret manager and must survive process restarts.
outbox = PgNotificationOutbox(
    pool=pool,
    sender=sender,  # SDK-owned RFC 9421 sender, or sender_resolver=resolver
    encryption_key=notification_encryption_key,  # exactly 32 bytes
    delivery_retry_horizon_seconds=86400,
)
await outbox.create_schema()

prepared = WebhookSender.prepare_account_status_changed(
    url="https://buyer.example/hooks",
    payload={
        "notification_id": "account-transition-42",
        "subscriber_id": "buyer-primary",
        "account_id": "acct_1",
        "previous_status": "pending_approval",
        "status": "active",
        "observed_at": "2026-10-02T12:00:00Z",
        "reason_code": "seller_approved",
    },
)
async with pool.connection() as conn:
    async with conn.transaction():
        await conn.execute("UPDATE accounts SET status='active' WHERE id=%s", ("acct_1",))
        await outbox.enqueue_prepared(
            conn, prepared,
            notification_type="account.status_changed",
            account_id="acct_1",
            caller_scope_id="seller-tenant-1",
        )

# Supervise this task in the application's worker lifecycle; cancel it on exit.
await outbox.run_worker()
```

Enqueue requires an open business transaction and performs no HTTP or credential
resolution. A rollback publishes nothing. A commit persists the immutable body,
key and headers for any later worker or process. `create_schema()` applies
`adcp/notification_outbox.sql`, which ships in wheels and source distributions;
production deployments can apply that additive DDL through their migration tool.
There are no changes to task or reporting tables. Custom table names are supported.

`caller_scope_id` must be the publisher's trusted caller/tenant identity, selected
from server-side state. Never copy it from untrusted payload metadata. Account
notifications also require the matching `account_id`. Caller-anchored principal
and capabilities notifications use `account_id=None`; they need no fabricated
account. Signing scope is an independently selected trusted key-service identity.

The uniqueness scope is `(caller_scope_id, account_id, destination,
notification_type, idempotency_key)`. The table indexes a SHA-256 digest of the
first four fields so large destinations stay within PostgreSQL's index limits.
Identical re-enqueue returns the original row ID without renewing the deadline.
Different callers/accounts/destinations/types can use independent copies of a key.
Reusing the same scoped key with different body, headers or signing scope fails.
Use cryptographically random keys per fire, even though isolation permits reuse.

AES-256-GCM encrypts the body and headers. The distinct `notification-outbox-v1`
authenticated metadata binds type, destination, account, caller, signing scope,
key, encryption-key ID and deduplication scope. Replay verifies the schema and
body's key/type/account bindings again before resolving credentials or sending.
Storage tampering fails authentication or binding validation and enters `invalid`
without delivery. Known notification shapes are schema-validated at enqueue;
extensions through `prepare_raw` must carry a matching `notification_type`.

For legacy envelopes without `notification_type`, use these routing labels:

| Preparation method | Outbox `notification_type` |
| --- | --- |
| `prepare_collection_list_changed` | `collection_list_changed` |
| `prepare_property_list_changed` | `property_list_changed` |
| `prepare_artifact_webhook` | `artifact-webhook` |
| `prepare_revocation_notification` | `revocation-notification` |

Workers claim one row with `FOR UPDATE SKIP LOCKED`, commit an expiring lease,
and perform HTTP outside the transaction. A worker killed before acknowledgement
leaves the row `in_flight`; another worker reclaims it after lease expiry. The
worker bounds credential resolution plus HTTP by the lease duration and rechecks
the lease and deadline immediately before HTTP. Default leases are 60 seconds;
they must exceed the resolved sender's HTTP timeout by at least five seconds.

Delivery is **at least once**: a receiver may accept a request just before its
worker crashes. Every retry replays the exact bytes, headers and delivery key,
with fresh signature metadata. Receivers must deduplicate by `idempotency_key`
and a trusted authenticated publisher identity that survives signing-key rotation,
and return 2xx for duplicates. Notification order is not guaranteed. Snapshot and
change-feed reconciliation remain authoritative recovery paths.

The retry horizon is 1–7 days **from enqueue**, including backlog time. Pending or
abandoned work expires at that deadline. Connection failures, timeouts, transient
DNS failures, temporary resolver failures, HTTP 408/425/429 and 5xx use exponential
backoff with optional jitter from `RetryPolicy`. `max_attempts` is ignored for this
durable path: the deadline governs retries. Other non-2xx responses, permanent URL
or hook validation failures and permanently unknown signing scopes quarantine the
row. Error columns contain bounded local discriminators, never peer bodies,
credentials or resolver/hook diagnostics.

`purge_expired()` expires overdue work and removes terminal rows seven days after
their original deadline. `run_worker()` calls it periodically. This keeps duplicate
bindings and delivery evidence during the retry window and a further retention
window. Operational monitoring should query state, attempt count, deadline,
`last_http_status` and `last_error`. Inspect quarantined rows through a trusted
operator workflow; repair the cause before deliberately replaying with the
original binding. Worker-loop errors are retried and cancellation leaves leases
recoverable.

Operator logs from `adcp.notification_outbox_pg` report the exception class and
traceback frame locations (filename, function and line number) for delivery,
unexpected resolver and worker-iteration failures. They exclude exception text
and chains, frame locals and source-line snippets; raw `exc_info` is not attached.
This identifies failure sites without logging credentials, URLs or peer content
carried by exceptions. Cancellation propagates without a failure log.

These diagnostics are separate from persisted errors. `last_error` remains a
bounded class/status or quarantine discriminator, with no traceback metadata.
For example, an unexpected resolver failure logs its original class and location
before becoming the persisted `ScopeTransientlyUnavailable` retry marker.
Iteration/database failures do not replace row errors; abandoned leases recover
through the existing lease-expiry path.

Use exactly one `sender` or `sender_resolver`. Both require RFC 9421 signing,
SDK-owned IP-pinned transport, HTTPS destinations and private destinations disabled.
The sender/resolver is application-owned; the outbox does not close it. Legacy
HMAC/bearer one-shot senders can still prepare notifications, while this worker
uses the protocol's default RFC 9421 transport.

With `sender_resolver`, every enqueue requires `signing_scope_id`; fixed-sender
outboxes require it to be absent. Each attempt calls the existing
`WebhookSenderResolver.resolve(scope)` and validates its `WebhookSenderResolution`
including advertised algorithms and timeout. Persisted scope is never changed or
replaced by another tenant's default. Rotate signing keys by updating resolution
under that same scope. `ScopeTransientlyUnavailable` retries;
`ScopePermanentlyUnknown` quarantines. Unexpected resolver exceptions are treated
as transient without persisting their messages.

For encryption-key rotation, deploy workers with both old and new keys first,
then select the new `encryption_key_id` and `encryption_key` for new rows. Supply
old keys through `decryption_keys={"old-key-id": old_key}`. Existing rows retain
their original key ID and remain readable and deduplicated. Retain each old key
until all rows encrypted under it have been purged. Unknown key IDs or wrong key
material quarantine rows; restoring a key does not automatically release them.
