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
Native versions retain their decoded provider value, including characters such as
`/`, `+`, and `=`. They are not entity IDs, URLs, or URI-encoded object references.

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

One logical revision may fan out to another destination obligation only when its
account, frozen period and resolved buy/package scope, coverage, definition and
currency match exactly. That does not grant another principal access: each
consumer still requires its own trusted binding and obligation delivery record.

Billing requires an official configuration, consumer receipts, and canonical digest
verification. Other managed profiles retain their narrower assurance: native commits
and manifest checksums do not assert cryptographic equality of logical rows.
Available and delivered claims follow the frozen binding's success status.

Adjustment acceptance verifies the complete adjustment's JCS/SHA-256 evidence,
including optional `reason_detail`, and its exact official revision. Correction
observation must be no earlier than finalization and no later than creation; receipt
observation follows creation. Periods must be ordered. These are evidence records,
not permission to post accounting entries, reopen books, change invoices, or settle.

Two wire boundaries need attention in the completion PR:

* The adjustment schema defines an independent receipt chain and does not require
  prior acceptance of the official revision receipt. The store records those two
  facts independently; completion must require both. An accepted adjustment alone
  cannot establish reconciled completion.
* The existing buyer `_select_current` can report `AMBIGUOUS_REVISION_CHAIN` when a
  retained snapshot and a separate official revision coexist, while Core's producer
  forbids an official revision from superseding a snapshot. This PR preserves that
  financial finality rule. The completion PR must resolve reader selection against
  the protocol before claiming a complete multi-finality lifecycle.

## Retention, snapshots, and future notifications

`read_reconciliation_snapshot(caller=...)` reads retained records at a Core ledger
boundary. Reusing its `boundary` excludes later outcomes, receipts and storage
checks. `get_materialization` returns the attempt, terminal outcome, binding and
readability history; `get_receipt` requires an explicit account/consumer key.
Expiry and corruption never erase immutable evidence or acceptance identity.
`readable_at()` applies the retained checks and exact resource expiry, rather than
changing the materialization's terminal wire state. A successful resource must last
through both the obligation floor and readiness plus the frozen retention contract.
There is no purge or unchecked history-repair API.

The opt-in `materialization_to_wire`, `receipt_to_wire`, `revision_to_wire`, and
`adjustment_to_wire` helpers support generated wire models. They are not mounted by
the Core status handler. Snapshot records are a lower-level storage read, not an
unbounded wire response; bounded status pagination and tier-correct counts belong
to the completion PR.

Each new record appends a distinct, scoped ledger change in the same transaction.
Pending attempts and terminal outcomes have separate change kinds, so a snapshot
cannot acquire terminal evidence committed after its boundary. Exact retries append
nothing. Both stores share the transition validator; PostgreSQL serializes with the
existing account lock and additionally uses a conditional receipt-head update.
Database triggers reject evidence updates/deletes and terminal-head replacement.
This is the transaction in which #1168 can insert its outbox row; there is no adopter
callback or after-commit webhook send in this PR.

## Migration and operational limits

Run `create_schema()` or all four bundled SQL resources in one transaction:
`reporting_ledger.sql`, `reporting_ledger_account_generations.sql`,
`reporting_ledger_obligation_currency.sql`, then `reporting_ledger_reconciliation.sql`.
The last migration is also one atomic statement in autocommit mode. All migrations
share the schema advisory lock. Drain older writers before upgrading.

The migration adds nullable managed digest/total evidence with no default/backfill,
immutable record and receipt-head tables, and account-qualified reference indexes.
Literal beta.15 and #1171 upgrades preserve pre-existing rows, hashes, currency, leases,
issue history and feed sequence numbers. Unknown currency/digest/total history stays
unknown. Existing Core replay hashes do not change. A different replay of an
existing adjustment now conflicts instead of silently returning its old content.

Table/index creation and prerequisite migrations take locks; production-sized
duration is not benchmarked. The reference stores load one consumer's retained
record set to validate transitions. Large histories need indexed queries or a
conforming replacement store before production rollout. Only SDK store operations
are supported writers; database owners can always circumvent application invariants
by disabling constraints. PostgreSQL connection ownership remains with the adopter.
