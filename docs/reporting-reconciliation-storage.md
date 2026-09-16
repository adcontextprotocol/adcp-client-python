# Seller reconciliation storage foundation

This is the durable storage half of #1167, based on #1174/#1169 and #1175/#1171.
It does **not** implement or advertise Managed Delivery or Reconciled Billing.
`ReportingStatusHandler` still projects Core only. Destination writers,
authenticated receipt handlers, batch idempotency, complete reconciliation health,
capability checks, and the transactional notification outbox remain follow-ups.

## Public replacement contracts

`adcp.reporting.ledger` exports `ReportingDestinationStore`,
`ReportingMaterializationStore`, `ReportingReceiptStore`, and their combined
`ReportingReconciliationStore`. They are additive protocols; an existing
`ReportingLedgerStore` implementation need not implement them.

`InMemoryReportingReconciliationStore` and `PgReportingReconciliationStore` also
implement the Core store protocol. Their constructors are the existing Core
constructors: an optional clock, and a caller-owned connection pool for PostgreSQL.
No destination, credential resolver, receipt client, writer, or notification sender
is required at construction or startup. Existing Core classes remain usable.

The methods return `(immutable_record, recorded)`. An exact retry returns the
retained record and `False`; reuse of an identity with different content raises
`LedgerConflictError`. Its `code` is a stable classification; messages do not echo
input, provider responses, SQL details, or credentials. Receipt `received_at` is
assigned by the store. Retry either the original input or the returned record;
changes to a supplied retained timestamp also conflict.

| Record | Write | Identity and transition |
| --- | --- | --- |
| `ReportingDestinationBinding` | `put_destination_binding` | Consumer plus typed account/configuration generation; immutable |
| `ReportingObligationDeliveryRecord` | `bind_obligation_delivery` | Account, consumer, obligation; freezes the existing obligation currency and retention floor |
| `ReportingMaterializationAttempt` | `commit_materialization_attempt` | Account, consumer, materialization ID; consecutive attempts per obligation/revision |
| `ReportingMaterializationRecord` | `commit_materialization` | The attempt's single terminal outcome: available, delivered, or failed |
| `ReportingMaterializationCheck` | `record_materialization_check` | Account, consumer, check ID; append-only, ordered readability/corruption observations |
| `ReportingRevisionReceiptRecord` | `record_revision_receipt` | Account, consumer, receipt ID; one current leaf per obligation/revision across all attempts |
| `ReportingAdjustmentReceiptRecord` | `record_adjustment_receipt` | Same receipt ID namespace; one current leaf per account/consumer/adjustment |

Receipt replacement names the current rejected leaf. Missing, stale, cross-account,
and cross-consumer replacement pointers have the same unavailable result. An accepted
leaf is terminal. History is never edited to mark a receipt superseded: current
leaves and terminal acceptance keys are derived from the retained graph. Revision
and adjustment receipt IDs share a namespace, as the batched wire request requires.

A retained snapshot and a later official revision coexist as independent histories.
The official revision does not supersede the snapshot. Each revision starts its own
materialization attempt sequence and receipt chain, so both can have attempt `1`
and a terminal acceptance. Accepting the official receipt leaves an earlier rejected
snapshot receipt replaceable; neither chain changes the other's retained evidence.
There is no mutable current-revision pointer in these storage contracts.
Consumers select the obligation's unique current revision before selecting a
materialization. An official revision wins when present. Otherwise the unique
unsuperseded snapshot is current. An unmaterialized, pending, or failed current
revision never falls back to an older materialized snapshot; multiple current
candidates fail closed.

## Trusted inputs and credential boundary

These are low-level seller storage contracts, not an authentication boundary.
Only trusted account/configuration ingestion may call `put_destination_binding`
and `bind_obligation_delivery`. The later receipt handler must derive
`ReportingDeliveryPrincipal` and `ReportingDeliveryScope.consumer_id` from transport
authentication and authorize the account; the receipt body cannot grant that access.
The bindings then constrain every materialization and receipt lookup and write.
`get_destination_binding` requires both caller and typed generation;
`get_obligation_delivery` requires the full consumer/account/obligation scope.

Use `ReportingConfigurationGenerationKey` from #1169 throughout. A binding adds
the consumer principal to that account-qualified generation. Configuration IDs,
materialization IDs, destination references, and receipt IDs may be reused by
independent principals/accounts without aliasing their retained state. Core revision,
obligation, and adjustment IDs retain the predecessor's global uniqueness constraints;
every new reference to them includes its account, including database foreign keys.

`trusted_binding_ref` names an immutable trusted configuration and is omitted from
wire projections and record reprs. It is **not** a credential, an authorization grant,
or a mutable `latest` pointer. A later trusted resolver may obtain rotating credentials
behind that reference while preserving the destination/configuration identity. No
resolver output enters these records. There is no arbitrary metadata dictionary or
provider response field. Public metadata uses closed, frozen record types, tuples,
safe failure classifications and inert references. URL credentials, signed query
strings, bearer material and private keys are refused without echoing the input.
Adapters must supply public identifiers; syntax checks cannot establish
the provenance of an otherwise opaque identifier.
Validators follow each field's wire contract: seller-issued entity IDs retain
their schema grammar; provider locations, consumer load/commit IDs, reader feature
labels, decoded object keys and native versions retain spaces, `/`, `+`, `=` and
Unicode without rewriting. Object keys must be destination-relative. All these
fields reject recognizable credentials and URLs, including encoded forms, without
echoing the input. Native versions remain separate from object keys and are only
URI-encoded when a later adapter constructs its provider request.

Credential detection matches *shapes*, not substrings. A keyword such as `token`
or `authorization` only rejects a value when it stands alone as a word and
introduces something after it (`token=…`, `Bearer …`, `authorization: …`), so
ordinary operational names — `tokenized_inventory_daily`, `secretariat-report-v17`,
`authorization_metrics_v2` — persist unchanged. Independently, any
`?name=`/`&name=` query-parameter pair is refused in every reference field, because
presigned S3/GCS URLs and Azure SAS tokens carry their secret in parameters this
boundary cannot enumerate; `report.csv?sv=…&sig=…` never reaches storage even
though no keyword appears in it.

## Evidence and financial ordering

`ReportingRevisionRecord.canonical_content_digest` optionally holds a frozen
`ReportingCanonicalDigest`. This is the **managed logical-row** digest under an
immutable canonicalization contract, distinct from Core's fixed
`revision_content_sha256`. Neither a destination response nor an observed consumer
digest establishes the expected digest. The trusted publisher must verify the pinned
contract and compute it before committing the revision. This PR stores that fact;
the later writer must perform and attest actual destination verification.
New managed revision commits also verify Core's row binding and include their
finality timestamps and canonicalization contract in immutable replay identity.
`ReportingRevisionRecord.managed_control_totals` retains expected
`ReportingControlTotalRecord` values before destination work; it must be present
for materialization attempts, including an explicit empty tuple for no totals.
`ReportingAdjustmentRecord.managed_control_total_deltas` similarly retains the
complete expected delta evidence used in the adjustment digest. A trusted publisher
supplies these from its pinned definition and verified content. Their names/values
must match the existing Core pairs. They are optional for Core-only records.
Pass those expected totals as `control_total_evidence` to
`revision_content_sha256()` when preparing a new managed revision. This binds
the full totals actually exposed by status and exact reads under the same Core
four-field hash rule. Omitting the argument preserves the existing Core hashes.

Terminal success checks row count, exact named totals, the selected profile, every
provided canonical digest, declared format, reader compatibility, method/resource
kind, verification path, physical-object membership and native-version evidence.
`ReportingControlTotalRecord` retains value type and unit as well as name and value;
changing a receipt's monetary unit is a changed statement, never a cached success.
Declared monetary units are checked against #1171's frozen obligation currency
and pinned definition; omitted units inherit that context. Nonmonetary units and
explicit types (including a decimal total represented by `"5"`) remain unchanged.
Unknown historical currency or missing expected managed totals fails closed.

A revision belongs to exactly one frozen obligation identity. Every attempt,
outcome and receipt must retain that account, configuration generation and
obligation, even if another obligation has identical content, scope and currency.
This slice has no obligation aliases or cross-obligation fan-out. Each consumer
also requires its own trusted binding and obligation delivery record.

Billing requires an official configuration, consumer receipts, and canonical digest
verification. Other managed profiles retain their narrower assurance: native commits
and manifest checksums do not assert cryptographic equality of logical rows.
Available and delivered claims follow the frozen binding's success status.

| Method | Canonical digest | Manifest checksums | Native commit |
| --- | --- | --- | --- |
| File transfer | Supported | Supported | Supported |
| Dataset share | Supported | Rejected at binding | Supported |
| Warehouse materialization | Supported | Rejected at binding | Supported |

Billing permits only canonical digest verification. Native commit requires
`resource.immutability == "native_version"`, identical resource/verification version
references, and an exact representative-consumer or destination observation path.
File transfer additionally requires the committed manifest and all object checksums.

Adjustment acceptance verifies the complete adjustment's JCS/SHA-256 evidence,
including optional `reason_detail`, and its exact official revision. Correction
observation must be no earlier than finalization and no later than creation; receipt
observation follows creation. Periods must be ordered. These are evidence records,
not permission to post accounting entries, reopen books, change invoices, or settle.

The adjustment schema defines an independent receipt chain and does not require
prior acceptance of the official revision receipt. The store records those two
facts independently; completion must require both. An accepted adjustment alone
cannot establish reconciled completion.

## Retention, snapshots, and future notifications

`read_reconciliation_snapshot(caller=...)` reads retained records at an independent
account/consumer boundary, typed as `ReportingReconciliationSnapshotToken`.
Reusing its `boundary` excludes later outcomes, receipts and storage
checks. `get_materialization` returns the attempt, terminal outcome, binding and
readability history; `get_receipt` requires an explicit account/consumer key.
Expiry and corruption never erase immutable evidence or acceptance identity.
`readable_at()` applies the retained checks and exact resource expiry, rather than
changing the materialization's terminal wire state. A successful resource must last
through both the obligation floor and readiness plus the frozen retention contract.
There is no purge or unchecked history-repair API.

`ReportingReconciliationFeedStore` is a separate, optional replacement protocol;
neither existing Core nor external reconciliation stores need new methods.
Both reference stores implement `read_reconciliation_changes(caller=..., limit=...)`.
It returns immutable `(sequence, record)` changes, a fixed boundary, an optional
continuation cursor, and a checkpoint only on the final page. Every record kind, including checks and both
receipt kinds, appears exactly once in sequence order when continuing from the
last consumed position. Interleaving writes appear on the next walk. Foreign
accounts and consumers never contribute records or change another principal's
snapshot ID, count, cursor, checkpoint, or next sequence. Reconciliation writes
also leave Core's sequence, snapshot ID, record counts and checkpoint unchanged.

Persist a partial page's records and `ReportingReconciliationCursor` together;
`changes_checkpoint` is `None` until the walk is complete. `cursor=page.cursor`
continues the frozen walk across a process restart. Persist the final page with
its `ReportingReconciliationCheckpoint`, then use `changes_after=checkpoint` to
open the next walk. Repeating a position intentionally replays immutable records.
`ReportingReconciliationFilter` supports record kinds and an exact obligation ID.
Caller scope and filters apply before counting and limiting; PostgreSQL keyset-pages
by the caller-local sequence under the frozen upper bound.

Opaque tokens bind account, consumer, feed version, filter, lower/upper bounds and
the last emitted sequence/record key. Tokens carry no authority: caller identity
always comes from authenticated transport and is revalidated against retained
records. A Core checkpoint, foreign scope, changed filter, invalid bounds, or
inconsistent last key is rejected. Core's `LedgerRecordKind`, `LedgerPage`, required
store protocols and wire projection remain unchanged. The early PR spelling
`ReportingReconciliationChangeStore` is an alias for the optional feed protocol.

The opt-in `materialization_to_wire`, `receipt_to_wire`, `revision_to_wire`, and
`adjustment_to_wire` helpers support generated wire models. They are not mounted by
the Core status handler. Snapshot records are a lower-level storage read, not an
unbounded wire response; bounded status pagination and tier-correct counts belong
to the completion PR.

Each new record appends a distinct change in `reporting_reconciliation_changes`
and advances `reporting_reconciliation_heads` for its account/consumer in the same
transaction. The memory store publishes the evidence and caller-local sequence
with one assignment under its lock. Core's change feed is separate.
Pending attempts and terminal outcomes have separate change kinds, so a snapshot
cannot acquire terminal evidence committed after its boundary. Exact retries append
nothing. Both stores share the transition validator; PostgreSQL serializes with the
existing account lock. Database triggers validate principal, generation, obligation,
revision, attempt and materialization references; receipt chains require the exact
current rejected predecessor. An insert advances its receipt head atomically in
the database, including for direct SQL writers. Composite foreign keys bind the
exact publication and adjustment graph. Bidirectional composite foreign keys require
the record and exact feed identity together; a deferred head reference and ordinal
guards prevent missing, skipped or reassigned sequences. Reads validate both sides
of the scoped feed/record join before boundary/count/limit so damage cannot disappear
through filtering. Stored identity columns are compared against the decoded payload.
Updates/deletes and terminal-head replacement are rejected; referenced
Core publications stay frozen while leases and readability remain operational.

The database does not take the retained payload or its fingerprint on trust.
`reporting_canonical_json` implements the `canonical_json_utf8_v1` profile in SQL, so
every insert recomputes `content_sha256` from the stored bytes and a closed key
allowlist refuses any field outside the frozen record types — no credential,
provider response or metadata bag can be retained even by a writer that goes
straight to the tables with correct identity columns. `reporting_reconciliation_evidence`
then re-derives the financial predicate that decides money: a successful
materialization must match its Core revision's row count, typed control totals and
canonical digest, its binding's format, readers, method and success status, and its
retention floor; an accepted revision receipt must match that materialization's
profile, totals and profile-specific evidence inside the resource's readable window;
and an accepted adjustment receipt's digest is recomputed from the retained
adjustment columns. A payload that disagrees with its own fingerprint fails every
read for that principal — including pages whose filter would have skipped the
damaged row — rather than being silently excluded.

This is the transaction in which #1168 can insert its outbox row; there is no adopter
callback or after-commit webhook send in this PR.

Cursors and checkpoints bind the feed version, account, consumer, normalized filter
fingerprint, frozen bounds and last key, and a continuation restores its own frozen
boundary before the store looks at today's head, so an authorized inter-page write
is deferred to the next walk instead of invalidating this one. Like Core's cursors
they are opaque but **unsigned**: the store re-validates caller, scope, filter and
bounds on use. A principal can therefore present a boundary it built itself, but it
gains nothing it could not reach through the supported API — a checkpoint at the
current head is exactly the token a completed walk would have issued — and a
boundary whose content disagrees with the retained feed fails as
`INVALID_CHECKPOINT`, never as `REPORTING_HISTORY_CORRUPT`. Only a boundary this
store opened can report retained damage.

## Migration and operational limits

Run `create_schema()` or all four bundled SQL resources in one transaction:
`reporting_ledger.sql`, `reporting_ledger_account_generations.sql`,
`reporting_ledger_obligation_currency.sql`, then `reporting_ledger_reconciliation.sql`.
The last migration is also one atomic statement in autocommit mode. All migrations
share the schema advisory lock. Drain older writers before upgrading.

The migration adds nullable managed digest/total evidence with no default/backfill,
immutable record, receipt-head, feed and caller-head tables, and account-qualified reference indexes.
Literal beta.15 and #1171 upgrades preserve pre-existing rows, hashes, currency, leases,
issue history and feed sequence numbers. Unknown currency/digest/total history stays
unknown. Existing Core replay hashes do not change. A different replay of an
existing adjustment now conflicts instead of silently returning its old content.
Upgrading the initial reconciliation schema preserves every existing record,
receipt head and legacy Core-feed row. It projects the retained legacy change order
into dense, independent account/consumer sequences without changing original hashes
or timestamps. The new reconciliation tokens do not reuse legacy/Core checkpoints;
start with a reconciliation snapshot or initial feed walk. The migration validates
all retained graph edges and feed identities and rejects incomplete history atomically;
it never repairs missing evidence or reassigns an obligation identity.

Table/index creation and prerequisite migrations take locks; production-sized
duration is not benchmarked. The reference stores load one consumer's retained
record set to validate transitions. Feed pages use indexed keyset reads, with scoped
integrity/count scans; these reads share the account transaction lock with writes.
The integrity scan recomputes one canonical digest per retained record for the
calling principal, and each insert recomputes its own, so both costs grow with a
principal's history and are not benchmarked at production sizes.
Large histories need benchmarks or a conforming replacement store before production rollout.
Only SDK store operations
are supported writers, but ordinary direct SQL — inserts that satisfy every column
constraint without disabling a trigger — can no longer retain extra payload
metadata, a fingerprint its payload denies, or financial evidence the Core revision
denies. A database owner who disables constraints outright can still corrupt the
tables; reads then fail closed for that principal. PostgreSQL connection ownership
remains with the adopter.

The database canonicalizer sorts object keys by bytes, which equals the JCS
UTF-16 order for the ASCII field names these records use, and reproduces
`datetime.isoformat()` for the recomputed adjustment digest. A payload outside that
domain simply fails its digest instead of being accepted. `ReportingRevision` carries
no obligation reference on the wire, so the buyer selector associates a revision with
an obligation through materialization ownership; two Core-only obligations that share
a definition, profile, campaign set and period still fail closed as ambiguous rather
than guess. An authoritative page-local ownership projection belongs to the status
slice, not to this storage slice.
