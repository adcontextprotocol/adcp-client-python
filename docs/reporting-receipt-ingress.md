# Authenticated durable reporting receipts

`adcp.reporting.receipts.ReportingReceiptHandler` mounts the experimental
`sync_reporting_receipts` task over the SDK's MCP and A2A transports. It accepts
revision and official-adjustment receipts, persists each item outcome, and
replays the original completed response after a process restart. Use
`PgReportingReceiptStore` in production. It extends the existing durable
materializer store; no second materializer or required legacy store method is
introduced. The memory store is a conformance/reference implementation.

This is **B2.2 of 4 within B2 of B1/B2** (Refs #1167). B2.3 owns frozen authorized
combined feeds, checkpoints and dependency closure. B2.4 owns versioned public
status, private ownership projection, complete ownership-walk validation and
per-offering production capability proof. Installing this slice does not make
Managed Delivery or Reconciled Billing ready. The existing new-store notification
advertisement veto also covers Core `reporting.ledger_changed`; Core polling is
unaffected. A deployment needing current Core notification advertisement may
retain the reviewed Core store for that surface. B2.4 owns deliberate admission
of the new store once all actual components and mounts are proven ready.

## Composition and identity

[The typed example](../examples/reporting_receipt_ingress.py) composes the store,
live account authorization callback and optional buyer registry, then uses
`serve(transport="both", auth=..., context_factory=auth_context_factory)`.
The same store participates in
[the durable materializer composition](../examples/reporting_durable_materializer.py).
A token validator supplies trusted principals. A registry-backed API/OAuth or
signed-agent adapter supplies trusted `AuthInfo` and its credential reference;
the registry is consulted again on every call, including exact replay.

`resolve_account(reference, context, canonical_consumer)` must resolve the exact
account reference and check the current application ACL. Return the canonical
storage account ID; unknown and denied accounts raise
`ReportingReceiptError("UNAUTHORIZED")`. An opaque account ID is sufficient;
a natural account key is resolved through the same callback. Authorization must
not be cached solely by the batch key. A hydrated `RequestContext.account.id`
must equal the resolved account.

The SDK cross-checks all present trusted principals, `AuthInfo`, signed agent
identity and active `BuyerAgent.agent_url`, using the reviewed canonical consumer
resolver. A raw `ToolContext.caller_identity` may identify the consumer.
`RequestContext.caller_identity` is an **opaque AccountStore cache key** and is
never parsed as a consumer. `tenant_id` and body fields cannot supply identity.
Absent, anonymous, inactive or conflicting identities fail closed. Two consumers
in one account and one consumer in two accounts have independent batch keys and
receipt visibility.

## Wire and replay contract

Requests negotiate `adcp_version: "3.2-rc.3"`. This is the wire release spelling;
`3.2.0-rc.3` is the bundle's semantic-version spelling. Unnegotiated traffic keeps
the SDK's existing AdCP 3.0 behavior; unsupported versions remain unsupported.
Pinned, unpinned and fallback MCP inventories use an isolated schema overlay.
Cached upstream schemas, generated models and older required protocols remain
unchanged.

Before any batch or receipt mutation the SDK validates the entire request:

- Supply `receipts`, `adjustment_receipts`, or both. Every supplied array is
  nonempty; the combined size is 1–100. IDs are unique across both arrays.
- Do not supply `received_at`. PostgreSQL assigns it inside the item transaction.
- Follow the exact receipt schema, including evidence and timestamp formats.
  Reporting's canonical JSON profile uses safe integers and decimal strings;
  nonintegral numbers, nonfinite numbers, duplicate JSON keys, unsafe integers and
  ambiguous requests and strings outside PostgreSQL JSONB's Unicode domain
  (U+0000 or surrogate code points) are rejected. Receipt-specific SQL hashing
  preserves the canonical UTF-16 key ordering for all accepted Unicode keys in
  `context` and `ext`; earlier schema functions and manifests stay unchanged.
- Account and canonical consumer come from the trusted resolver and context.
  Request identity fields cannot override them.

Malformed shape produces task-level `INVALID_REQUEST` with zero writes.
Semantic failures are durable item results. Unknown and unauthorized referenced
records use the same safe `REPORTING_RECORD_UNAVAILABLE` item shape. Results are
revision receipts in input order followed by adjustment receipts in input order.
A failure of either kind has top-level `reporting_receipt_id` and `errors`;
a success has `receipt` or `adjustment_receipt`, including server `received_at`.

The immutable batch header is keyed by `(account, canonical consumer,
idempotency_key)`. It stores the canonical **whole request**, digest and expected
count; `context` and `ext` participate in identity. Every ordinal, including a
failure, is durable. The final response is persisted and validated against the
original ordered items. Changed content conflicts before further work. Replays
return original `recorded`/failure results and original server timestamps, without
adding `replayed` or applying a response enhancer.

The SDK generic idempotency wrapper and middleware are bypassed for this task:
the generic cache lacks the resolved account and can bypass revoked
authorization. Application middleware must not independently cache receipt
responses or rewrite admitted receipt parameters. Receipt pre-validation hooks
may inspect an isolated copy, but changing its content is rejected.

The production HTTP mounts retain bounded raw bytes in the current request's
ASGI scope because A2A protobuf `Struct` normalizes JSON integers to floats.
The A2A wrapper checks a uniquely identified receipt's Unicode encoding before
protobuf decoding. Receipt parameter extraction remains bound to the selected
route and normal transport authentication; account and consumer authorization
still run in the handler. Raw bytes are never sourced from client metadata,
shared context caches or request-ID lookups. Both the normal body limiter and
the receipt capture bound apply. The raw request must identify
one unambiguous standard receipt invocation; lossy direct/custom transport inputs
fail closed. All normal whole-shape, numeric, account and consumer checks still
run. This does not replace the A2A stack or alter other tasks' version handling.
Exact integral HTTP numeric spellings such as protobuf's `1.0` are decoded from
their original text without floating-point rounding and canonicalized as `1`.
An already rounded direct/custom parameter value cannot supply that evidence
and is rejected. Fractional text that would round to an integer is also rejected.

## Financial evidence and transactions

Revision receipts match the exact account, consumer, obligation, revision,
materialization, verification profile, digest and control totals. Acceptance
requires readable evidence for that artifact at observation. Receipt chains are
single, complete, acyclic chains with no cross-target edges. Only the current
rejected leaf may be replaced. An accepted leaf is terminal. Adjustment receipts
match the exact official revision, canonical adjustment digest, finality and
correction/creation/observation ordering. PostgreSQL predicates also enforce these
relationships for ordinary SQL inserts.

An accepted receipt remains authoritative for its referenced immutable artifact
after another materialization succeeds or fails, or after that artifact expires
or becomes corrupt/unreadable. Current readability is assessed separately.
Acceptance does not promise current availability; later health does not rewrite
acceptance. Consumer rejection never allocates a materializer attempt or dirties
retry work.

For each item the account advisory lock is acquired before the batch row lock.
One connection-bound transaction chooses the next durable ordinal and commits
its receipt, private caller feed entry, ordinary status-dirty behavior, required
immutable captured status input and exact ordinal outcome. A semantic failure
commits only that ordinal outcome. A storage/capture/result failure rolls the
whole current ordinal back; previously committed sibling outcomes remain.
Memory rollback is unconditional, including first-use collections and sequence
heads, with notifications both off and on.

Captured receipt inputs are isolated epoch-zero records with their own caller
sequence and B2.1's existing account capture sequence. They retain exact private
consumer and obligation/revision dependencies even when consumer status is
disabled. They do not activate a public projection, release historical events or
produce readiness. B2.1's terminal outcome + captured status input + work ACK +
enabled logical enqueue remains the original transaction. Its preactivation
work/events stay permanently quarantined across replay and later activation.
Ordinary terminal materialization writes still mark status dirty without
readiness, and argument bounds remain actionable closed errors.

## Migration, rollout and recovery

1. Back up and migrate with `await store.create_schema()` using a deployment
   connection. The additive `reporting_receipt_ingestion.sql` and independent
   `receipts/required_schema.json` ship in both wheel distribution paths. Do not
   modify A/B/C manifests or the **187-object** B2.1 materializer manifest.
   This migration also carries an additive Core fix: `reporting_configurations`
   gains a defaulted `lease_turn` column, a monotonic sequence and a distinctly
   named index that give period-close leasing a durable, total fairness order.
   Without it a worker that releases each turn can re-lease one generation
   forever and never close any other account's periods. Rows written by older
   writers default to the never-leased rank, so nothing is starved and no
   required manifest key changes.
2. Verify `await store.receipt_ingestion_ready()` on the actual installed store.
   Empty, old, partial, disabled-trigger and mismatched schemas fail closed,
   including completed replay. The migration is transactional, repeatable and
   serialized; interrupted installation exposes no partial schema.
3. Drain legacy materialization writers before autonomous materializer activation.
   Preserve [B2.1's legacy-pending recovery/import rules](reporting-durable-materializer.md).
   Never invent an external idempotency history or reuse quarantined epoch-zero
   evidence for later activation. Old ordinary writers are compatible during a
   controlled rolling deployment; they are not autonomous workers to run in
   parallel with the new service.
4. Mount the authenticated receipt handler and verify its actual MCP/A2A routes.
   Deploy the same durable store to restarted instances. Keep account resolution
   and registry revocation live. Do not advertise incomplete tier capabilities.
5. On timeout, cancellation, process death or uncertain commit, retry the identical
   body and key. The durable prefix resumes with no duplicated receipt or changed
   timestamp. Final-response failure retains earlier ordinals and rebuilds the
   response from them. `IDEMPOTENCY_CONFLICT` requires recovering the original
   submission; do not erase the batch to reuse its key.
6. For `RECEIPT_SCHEMA_UNREADY` repair the installed migration. For
   `RECEIPT_HISTORY_CORRUPT` stop ingress and investigate the immutable evidence;
   do not rewrite financial outcomes. `RECEIPT_STORAGE_UNAVAILABLE` is a safe
   storage boundary error; retry the original body after service recovery.
   Closed public errors omit provider bodies, credentials and SQL diagnostics.

Keep batches, ordinals and captures at least as long as their referenced receipt
history and replay obligation. This slice does not prune them. Database owners
can bypass SQL protections, so migrations and repair remain operator-controlled;
application roles must not disable triggers or rewrite immutable rows. Rollback
means draining the new ingress and returning to compatible older ordinary
readers/writers while retaining the additive objects and records. Do not drop
financial history as a rollback strategy. B2.4 must separately drain/fence
incompatible status projectors before its activation.

Historical reader/writer gates execute actual installed beta.15, records A,
foundation integration, outbox A, activity B, status C, B1 and the exact approved
B2.1 artifact on populated new records, comparing permitted old behavior before
and after this migration. Their reviewed PostgreSQL prerequisite is **UTF8/C
collation and ctype**. A's notification-readiness closure after C is compared on
both sides and does not excuse new regressions. Non-C probes are distinct from
this frozen-artifact gate. Optional notifications may stay explicitly disabled;
complete polling still depends on the later B2.3/B2.4 components. Full buyer
adjustment automation and `client.reporting` remain named downstream #1172 work.
