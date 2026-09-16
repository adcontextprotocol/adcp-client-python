# Verified destination I/O — #1167B1

**B1 of B1/B2**, stacked on reviewed #1168C at
`ea150fabd5ad90e3abf93f89729d2919f1c61798`. Refs #1167.

B1 supplies immutable public contracts, whole-history revision selection,
SDK-owned source/destination verification, and a deterministic development
destination. Import from `adcp.reporting.materializer` and
`adcp.reporting.revision_selection`; PostgreSQL is optional. The strict
[adopter example](../examples/reporting_destination_writer.py) accepts existing
frozen ledger records and returns verification observations.

There is no durable materializer, discovery queue, lease manager, retry
allocator, final outcome transaction, or materializer migration in B1.
`ReferenceReportingDestinationWriter.production_eligible` is always `False`,
including at the type level. It cannot be promoted by configuration or
subclassing. Configuring it, completing its readback, or freezing a destination
binding never advertises `managed_delivery`, `reconciled_billing`, or
`reporting.delivery_ready`. The real outbox readiness helper suppresses that
claim until B2 can prove a complete durable materializer. The producer's `extra`
argument rejects SDK-owned task, tier and notification keys.

## Trusted contracts and lifecycle

`ReportingDestinationRequest` includes the exact account and canonical consumer
(including HTTPS BuyerAgent identities), configuration generation, trusted
destination and binding references, binding fingerprint, obligation, revision,
materialization ID and attempt. Its verification key includes the complete
frozen definition/schema, canonicalization and capability tuples. Aliases must
be resolved by trusted middleware before this boundary. Credentials are never
protocol arguments, request fields, locators, verification records or metadata.

`ReportingWriterCapability` closes method, transport, format, profile, readback
path, immutable-location/native-version mode, SHA-256 checksum and conditional
or idempotent write semantics. An unsupported tuple fails before resolver or
writer I/O. The immutable registry holds explicitly installed contract bytes;
it has no registration mutation, plugin callback or network fallback.

A resolver synchronously constructs an **unopened** SDK
`ReportingDestinationSession`. Resource acquisition and fresh authorization
belong in `_open`; all credentials and partial acquisitions belong to that
redacted, non-persistable session. `_close` must release every acquired resource
and any adopter spool, including after an interrupted `_open`. Override the
protected hooks and I/O methods, preserving the SDK-owned context manager,
redaction and exactly-once close. Read methods must resolve locators inside the
session's trusted destination namespace and reject another tenant's paths.

Write and readback open separate sessions and independently authorize the same
frozen request, so rotation/revocation between phases is observable. The SDK
checks the resolved request before opening and before I/O. `ReportingIOContext`
carries an absolute UTC deadline, cancellation event and optional service-owned
heartbeat checkpoint. It uses Python-3.10-compatible primitives, joins canceled
tasks, shields bounded cleanup and re-raises a clean `CancelledError`. The
optional heartbeat is a call boundary; B1 starts no lease-extension loop.

Writers receive SDK-produced immutable canonical row bytes inside
`ReportingPreparedRevision`, never mutable row dictionaries or arbitrary
metadata. They return `ReportingDestinationLocator` claims. Counts, checksums,
manifest hashes and native commit IDs supplied by a writer establish where and
what to read; they cannot establish verification.

`ReportingWriterFailure` contains only a closed code, retry instruction,
retry-after seconds and external-effect state (`not_started`, `applied`,
`unknown`). Unexpected provider failures become closed diagnostics without
provider exception chains. Do not construct messages from provider text or
log session internals, signed URLs, credentials or provider bodies.

The external idempotency identity includes all tenant, consumer, binding,
revision and attempt coordinates. Resuming the same pending attempt preserves
it; equal attempt numbers on different revisions do not collide. Unknown
effects cannot request a new attempt. **Public foundation persistence still
allows N+1 after any immutable terminal outcome.** B2's autonomous retry rule
will be narrower: N+1 only after its own known terminal failure, never while N
is pending. Consumer receipt rejection is unchanged. Before activating B2,
drain legacy materialization writers and explicitly recover/import legacy
pending identities; their external-effect history cannot be inferred.

## Installed canonicalizer and destination verification

The small built-in implementation supports pinned `adcp_jcs_rows_v1` contracts,
Draft 2020-12 schemas with local fragment references, direct sum metrics and
complete integer/fixed-scale decimal control totals. Decimal values are strings;
JSON integers must be exactly typed and within the JavaScript safe range.
The pinned schema's `x-adcp-control-total` annotation specifies value type,
optional unit and decimal scale. All declared metrics must be represented.
The reference definition, row schema and canonicalization golden vectors are
bundled package assets with exact byte hashes in both distribution paths.

This deliberately bounded JSON subset rejects floats, subclasses, tuples,
Decimal/datetime/bytes/set values, duplicate JSON members, invalid UTF-8 and lone
surrogates. It preserves Unicode without normalization and applies JCS UTF-16
member ordering. Primary-key row ordering is by canonical scalar-key-array
bytes; duplicate primary keys fail. Golden vectors must test empty content,
nontrivial row ordering and member ordering. SHA-256 hexadecimal evidence is
validated and compared semantically, accepting uppercase and producing lowercase.

Preparation reads **every** frozen source page, including zero rows and 501+
rows, rederives the Core digest and the canonical digest, and recomputes typed
totals before destination authorization. SDK source cursors bind revision and
offset; custom row readers return the same `ReportingRowPage` identity/cursor
contract. Stable totals, cursor progress, cycles, `has_more` pairing and final
count are enforced. Source/destination walks bound bytes, recursive items,
nesting, rows, pages, objects and chunks.

Readback independently verifies every logical destination row in order. File
verification also reads the exact manifest bytes and its closed schema,
identities, period, creation time, complete typed totals, ordered object
inventory, and every streamed JSONL object's checksum, length, row count and
content. Native verification observes the pinned version, location and required
consumer/destination path before and after all pages; each page repeats that
version. A native commit ID alone cannot satisfy canonical-digest verification.

| Method | B1 reference format | Supported profiles | Required readback |
| --- | --- | --- | --- |
| File transfer | JSONL, uncompressed | canonical digest, manifest checksums | logical pages + exact manifest + every object |
| Dataset share | logical typed rows | canonical digest, native commit | representative-consumer rows + pinned native observations |
| Warehouse materialization | logical typed rows | canonical digest, native commit | destination rows + pinned native observations |

The verifier returns `ReportingVerifiedDestination` only after these reads.
Corruption returns a closed failure and no reusable verified result. The public
foundation's materialization transition validator checks retained claims; it is
not an independent destination reader. Its persisted `MaterializationFailure`
variants and record shapes are unchanged.

## Current revision and B2 finish seam

The neutral selector returns `selected`, `not_ready` or `corrupt`. It validates
ownership, duplicate IDs, every predecessor, finality edges, connected snapshot
history, forks, cycles and multiple officials before selecting. Only empty
history or absence of a required official is ordinary not-ready. A unique
official wins over an intact retained snapshot chain; otherwise snapshot
finality selects its one unsuperseded leaf. Readability and materialization
availability never select a revision or allow fallback.

Core health, producer acquisition, status projection/validation/lifecycle,
consumer planning and reconciliation use that selector. Producer corruption
fails before source or adapter I/O. Status emits a stable `HISTORY_UNAVAILABLE`
issue. The exported `current_required_revision(...) -> record | None` remains
source compatible; internal selection consumes the typed result.

`validate_materialization_target` is a pure seam for B2's locked finish path.
It checks the selected/readable exact revision and frozen binding against the
prepared input. It provides no transaction or fencing claim. B2 must reselect
on the same account-locked connection and co-commit immutable outcome,
reconciliation feed, work acknowledgment, C dirty and readiness notification.
A stale-after-I/O attempt must close as a compatible public safe failure with
its richer reason isolated in B2 state; B1 does not add a persisted
`CURRENT_REVISION_CHANGED` variant. Neither B1 nor B2 merges autonomously.

## C selector-epoch cutover

B1's only SQL addition is the separately manifested **C checkpoint** migration
`reporting_status_selector_version.sql`. See the
[C rollout instructions](reporting-status-notifications.md#selector-epoch-cutover-1167b1).
Stop and drain old C projectors and sweepers before enabling v2 turns. A/B/C
Core and notification writers remain compatible; legacy materialization writers
must separately be drained before B2 activation.

The frozen-C process gates exercise populated checkpoints, old `claim_due`,
old schema recreation, competing v2 projectors/sweepers, pool-local marker
cleanup and retained physical rows. Shared memory/PG vectors cover interrupted
fence, checkpoint/event and final-mark commits, ordered boundaries, late clocks,
retained scopes, unchanged fingerprints and once-only restart convergence.
