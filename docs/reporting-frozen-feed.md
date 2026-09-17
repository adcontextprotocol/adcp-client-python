# Frozen authorized reporting feeds

`PgReportingFeedStore` adds persisted `get_reporting_status(view="periods")`
walks to the existing receipt/materializer store. Pass it to
`ReportingReceiptHandler`, using the same trusted identity adapters and fresh
account resolver as receipt ingress. The existing
[`reporting_receipt_ingress.py`](../examples/reporting_receipt_ingress.py)
composition checks the actual optional feed protocol and schema. A
`PgReportingReceiptStore` instance continues to mount receipts only; both
handler instances can coexist in either construction order. Existing Core
handlers and low-level buyer complete-history inputs keep their behavior.

This is B2.3 of 4 within B2 of B1/B2. It does not activate Managed/Reconciled
status, the public revision-ownership extension, or higher-tier capabilities.
The new-store notification advertisement veto remains in place, including
Core `reporting.ledger_changed` advertisement. Core polling remains available;
existing eligible reviewed stores keep their supported Core notifications.

## Reading and consuming a walk

The handler resolves the canonical account and consumer on **every** request,
including continuation and checkpoint replay. A production RequestContext's
opaque account-cache key and transport tenant never identify the consumer.
API/OAuth/registry and signed-agent adapter identities must agree. Revocation
can deny an old page; it never reconstructs that page from current records.
The SDK's generic idempotency cache is bypassed for this optional mounted read.
Do not supply an `idempotency_key` on a feed request.

The public record set is Core obligations, revisions, adjustments and the
caller's consumer statements, plus the caller's terminal materializations,
revision receipts and adjustment receipts. Destination bindings, obligation
deliveries, pending attempts, checks and private captured inputs stay private.
Consumer privacy applies with `consumer_status_enabled` both off and on.
Core adjustments appear once.

The first page acquires the account lock, captures Core and caller histories,
both visible maxima and database `as_of`, and persists the **actual wire
records** and private historical inputs on that same connection. It never
settles an issue, acknowledges work, changes receipt replay or mutates a
notification queue. Partial capture, insertion or page assembly rolls back
the entire snapshot transaction. Memory follows the same unconditional rollback
boundary, including newly initialized collections and sequence heads.

Ordering is `(domain_rank, sequence, record_kind, record_id)`, with Core rank 0
and caller reconciliation rank 1. Each domain retains its original committed
sequence; unrelated sequence spaces are never collapsed into one maximum.
The persisted ordering is total and reproducible. Wire identity is the pair
`(record_kind, record_id)` within the authenticated account/consumer snapshot.
Visibility precedes maxima, counts and page limits. Foreign private writes
cannot change another consumer's visible vector or already-open walk.

Dependency closure replays an affected current owning obligation once and its
complete revision history, including older snapshot restatements and an
unlinked terminal official. Receipt/materialization/adjustment references
retain their exact targets even below `changes_after`. A current official
without its own materialization is never assigned an older snapshot's artifact.
Missing, conflicting or corrupt retained dependencies fail closed. Identical
scope metadata is never used to guess ownership. `pagination.total_count` counts
the final deduplicated wire set **after** closure; it stays identical on all pages.

Use the same semantic filters throughout a walk. `pagination.max_results` may
change (1–100), and `context` is echoed independently on each request. All
semantic fields, including vendor `ext`, bind the snapshot. Ordinary finite JSON
numbers are supported in context and vendor filters. Account aliases are bound
through the authenticated canonical account, not their spelling in the request.

Each page supplies one identical `changes_checkpoint`. Persist or advance it
**only after** `pagination.has_more` is false and all pages have been consumed,
including an empty final projected page. On interruption, resume the saved
cursor or replay the previous exhausted checkpoint; do not advance to the new
checkpoint simply because its bytes appeared on page one. Server-issued tokens
cannot prove that a buyer durably applied pages. This consumption rule and its
reference walk tests are B2.3's contract; new durable buyer submission automation
remains separately owned.

`rpf1` cursor/checkpoint tokens use a versioned compact body and a per-snapshot
HMAC key stored with the snapshot. Their binding hashes the canonical account,
canonical consumer, semantic filters, both after/through coordinates, `as_of`,
wire total, snapshot identity, complete frozen representation and last global
sort key. Principals and filter bodies are not embedded in the token. Generated
client round trips cover maximum supported 2,048-character URL principals and
enforce a 2,048-character token bound in actual MCP/A2A requests and MCP inventory
schemas, including pinned and fallback definitions. Legacy unbound checkpoints
and unavailable snapshots return `INVALID_CHECKPOINT`; restart a complete walk.
Frozen positions are valid only for the periods view. Changing to a summary or
revision view rejects them before consulting the legacy current-state projector.

## Versioned private inputs and B2.4 continuation

Version 1 stores the original Core configuration/obligation/revision/issue and
consumer-status snapshot, all caller reconciliation history and sequences, the
retained B2.1 materializer and B2.2 receipt boundaries, exact revision ownership,
complete selections/history, readability at capture, seed membership and exact
dependency edges. Trusted destination references remain private. Capture rejects
conflicting owners or changed immutable records across retained boundaries;
readability may evolve without rewriting earlier evidence. Accepted receipts
remain attached to their original artifacts after later success, failure,
unavailability, corruption or expiry. Rejected receipt evidence and its exact
replacement chain are retained without triggering a materializer retry.
Receipt admission uses its committed caller-feed prefix, so a later check with
an older observation timestamp cannot rewrite acceptance. Current readability
is captured independently from the complete history. A legacy Core consumer
statement that names a revision but omits its obligation ID retains its original
wire bytes; closure follows that exact revision's authoritative owner.

`representation_version=1`, private `projection_version=1` and
`ownership_mode="absent"` identify this feed representation. They are not a
status activation or production admission certificate. Every existing snapshot
retains its wire bytes, membership, ordering, counts, checkpoint, private inputs
and absent ownership mode through later activation. The SQL table forbids
updates/deletes and version retagging; no public ownership extension is emitted,
including on empty pages. B2.4 must retain this decoder and serving path and may
use isolated additive objects for a new representation on new snapshots. It must
prove continuation using the actual approved B2.3 binary/pages through migration
and restart. It cannot alter objects in this version's exact readiness manifest.

B2.4 owns opt-in public ownership serialization and complete extension-walk
validation, versioned reconciliation status/projector fences, tier-correct
definitive/count/retention views, offering-specific capability admission and the
final integrated Core → Managed → Reconciled lifecycle. These remain closed.
The final seller acceptance stays inside complete B2; the separately owned buyer
automation/facade and pinned cross-language program remain #1172 prerequisites.

## Migration, rollout and recovery

1. Back up retained reporting history and keep the approved B2.2
   [receipt-ingress operations contract](reporting-receipt-ingress.md) and
   [materializer recovery contract](reporting-durable-materializer.md).
   Drain legacy autonomous materialization writers before activating the durable
   worker; ordinary old readers/writers in a controlled rollout do not authorize
   competing legacy autonomous workers. Preserve explicit legacy-pending import
   and original external effect identity. B2.4 will additionally own incompatible
   projector/sweeper drain and activation.
2. During deployment, call `await store.create_schema()` with the deployment
   connection or pool **before** starting workers or mounting the new feed.
   The same additive bootstrap retains the private turn-primary fairness objects
   `adcp_reporting_configuration_lease_turns`, outside the enumerated catalog.
   An unmigrated first lease fails loudly; a generation without a turn is treated
   as not yet leased. Do not add lease-turn columns to the frozen configuration shape.
   Leave the production application-clock override unset for DB-timed workers.
3. Check `await store.reporting_feed_ready()` and
   `await store.receipt_ingestion_ready()` in the typed composition. The isolated
   feed manifest contains 33 objects. It adds to the preserved 453 ledger objects,
   187 materializer objects and 102 receipt objects: 775 enumerated objects when
   those features are installed. Private fairness objects remain outside these
   counts. No prior manifest acquires feed objects. Missing/partial/mismatched
   objects refuse new snapshots and continuation; no silent legacy fallback.
4. Mount the same authenticated handler on MCP/A2A. New snapshots use the new
   store; receipt-only handlers retain their prior inventory and unsupported
   feed behavior. Schema installation is transactional, serialized, repeatable
   and safe to retry after interruption. It preserves pending work, lease turns,
   original receipt ordinal/final responses and server timestamps, captured
   boundaries, immutable outcomes and already-open snapshots.
5. On application rollback, leave additive objects and retained data installed.
   Actual frozen historical binaries exercise their permitted ordinary reads
   and writes on the new schema. They do not serve B2.3 cursor tokens; route
   continuations to a compatible feed reader until walks finish. A missing or
   damaged snapshot requires operator investigation and conservative full replay,
   not a reconstructed projection. This slice does not prune snapshots or
   accepted evidence; plan storage capacity for retained histories.

The materializer finish and receipt ordinal transactions are unchanged: snapshot
reads consume committed history under the same account-lock ordering. Permanently
quarantined epoch-zero readiness records remain quarantined after restart,
pending resume, replay, migration and rollback. Disabled notifications enqueue
nothing; an enabled path retains its original atomic enqueue/rollback contract.
Feed readiness does not release these higher-tier activation gates.
