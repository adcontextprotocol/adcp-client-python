# Definition-Driven Official Reporting Finality

Status: approved for implementation  
Tracking: AI-22659

## Context

The reporting producer can currently emit an official revision without retaining the
specific finality policy selected from the pinned report definition. It derives a
`finality_basis` from source-manifest evidence and constructs a synthetic
`finality_policy_id`. That is not sufficient for AdCP official finality: the emitted ID
and basis must name an exact policy in the content-addressed report definition, and the
seller must prove the requirements of that policy before publishing the irreversible
official close.

The TypeScript SDK already models an explicitly pinned `finalityPolicy` for
`source_final` and `contractual_cutoff`. The Python model will use the same policy
shapes and add the protocol's third `stabilized` basis. A later TypeScript change can
add the same third shape and proof flow without translating between different policy
concepts.

## Goals

- Require an explicit report-definition policy for new official reporting.
- Support `source_final`, `contractual_cutoff`, and `stabilized` without inventing an
  SDK default.
- Emit the exact selected policy ID and basis on official revisions.
- Derive contractual and stabilized finality from verified snapshots; only
  `source_final` requires an authoritative upstream signal.
- Retain enough durable observation evidence to prove stabilization across restarts.
- Preserve snapshot scheduling, unchanged-content suppression, reconciliation,
  materialization, and status behavior outside the official-finality decision.

## Non-goals

- The SDK will not choose among multiple policies in a report definition.
- The SDK will not treat the existence of `finality_policies[]` as a request for an
  official close.
- The SDK will not infer a policy from source-manifest evidence, an offering ID,
  `official_close_lag`, or a source-provided `stable_since` value.
- This change will not modify the TypeScript SDK. Its matching `stabilized` support is
  separate work.
- The SDK will not approximate calendar-relative years or months as fixed durations.

## Policy Model

Add a frozen, public Python policy union with protocol field names:

- source-final policy: `finality_policy_id`, `basis="source_final"`, `source_signal`
- contractual-cutoff policy: `finality_policy_id`,
  `basis="contractual_cutoff"`, `duration_after_period_end`
- stabilized policy: `finality_policy_id`, `basis="stabilized"`, `minimum_age`,
  `unchanged_for`

The selected policy is a trusted projection from the content-addressed report
definition. It is retained with `ReportingDefinitionBinding`, which already stores
trusted non-wire projections of that definition. This keeps the selected policy in the
existing configuration and obligation definition JSON rather than changing the shared
PostgreSQL configuration/obligation column layout. `to_wire()` remains unchanged;
`to_storage()` includes the selected policy.

There is no default policy and no automatic selection. New production admission
requires a selected policy when `required_finality` is `official`. A snapshot
configuration does not select a policy unless it explicitly enables an official-close
path. An explicit snapshot-to-official path without a selected policy fails admission
instead of emitting a synthetic policy ID.

Policy identifiers must satisfy the report-definition identifier constraint. Durations
must use the SDK's exact day/hour/minute/second profile. A policy containing calendar
years or months is valid AdCP data but unsupported by this fixed-clock producer and is
rejected during admission rather than approximated.

## Admission and Source Routing

Production offering validation uses the selected policy to prove that the configured
source can supply the needed evidence:

- `source_final` requires an `AuthoritativeOfferingV1` and the configured official
  source offering.
- `contractual_cutoff` requires a `ProvisionalSnapshotOfferingV1` and the configured
  snapshot source offering.
- `stabilized` requires a `ProvisionalSnapshotOfferingV1`, durable provisional
  observation support, and the configured snapshot source offering.

The public delivery offering must still advertise support for the configuration's
required finality. The source route is an implementation detail: a seller may publish
an official revision derived from a snapshot when the selected report-definition
policy, rather than the source, establishes finality.

The selected policy is copied unchanged from configuration to obligation through the
retained definition binding. Restarts and configuration successors therefore cannot
reinterpret an existing obligation under a different policy.

## Runtime Flows

### Source-final

The producer waits for the authoritative source offering, requests an authoritative
publication, and validates all existing manifest and coverage invariants. It additionally
requires:

- finality evidence observed no earlier than the period end;
- an authoritative evidence basis (`provider_declared` or `provider_job_terminal`);
- `evidence_ref` equal to the selected `source_signal`.

The official revision uses the selected policy ID, `source_final`, and the source
evidence observation time as `finalized_at`. A mismatched signal is a manifest mismatch,
not a reason to substitute another policy.

### Contractual cutoff

The producer does not dispatch the final read before
`period.end + duration_after_period_end`. At or after that boundary it requests a full,
verified snapshot through the snapshot offering. A successful read is published as an
official revision under the selected contractual policy.

`finalized_at` is the trusted producer publication time after the qualifying acquisition
completes. This avoids backdating finality to a cutoff at which the SDK had not yet
verified the published content. Source availability and retry rules remain in force; a
cutoff makes a close permissible, not magically available.

### Stabilized

The producer begins verified snapshot observations when the snapshot source is
available. Every successful observation is retained, including unchanged rereads. The
existing content-change suppression remains in force: an unchanged observation advances
the durable observation ordinal and history but does not create another snapshot
revision.

A stability run begins when the current snapshot content is first observed. It continues
only while:

- each later observation has the same content fingerprint and immutable row binding;
- no scheduled observation is missed; and
- the current snapshot revision and observation history remain readable.

A content change or a gap past the frozen next-observation deadline starts a new run.
Source-provided `stable_since` is retained as source evidence but never establishes the
run.

The content qualifies when the latest retained observation is at or after both
`period.end + minimum_age` and `run_start + unchanged_for`. The producer then publishes
the verified current content as an official revision with the selected policy ID,
`stabilized`, and the qualifying observation time as `finalized_at`.

The final qualifying observation is committed before the official revision. A crash
between those commits can delay the close but cannot publish one without durable proof;
the next worker turn resumes from the retained history and performs an idempotent close.

## Durable Observation Evidence

Extend the provisional observation store protocol with a bounded, account-scoped list
operation for one obligation. Memory and PostgreSQL stores already retain observations
by observation ordinal, so no schema migration is required. The PostgreSQL implementation
will return them in ordinal order from the existing immutable observation table.

The stabilization evaluator consumes this retained sequence and validates each record
against its referenced snapshot revision. It does not trust a derived checkpoint alone.
The current restatement checkpoint remains the scheduling cursor; the observations are
the proof.

Observation listing is bounded by the policy interval and cadence. The evaluator asks
only for the suffix needed to cover `unchanged_for`, plus the immediately preceding
observation needed to establish the run boundary. Store implementations must not scan
unrelated accounts or obligations.

## Revision Construction

Official revision construction receives a validated selected policy and a proven
`finalized_at`; it no longer derives either from the offering ID. The revision fields are:

- `finality="official"`
- `finality_basis=selected_policy.basis`
- `finality_policy_id=selected_policy.finality_policy_id`
- `finalized_at=the policy-specific proven instant`

All existing content hashing, source publication references, control totals, row staging,
terminal-official behavior, and adjustment-only corrections remain unchanged.

## Error Handling

Admission fails before obligations are created when:

- official finality has no selected policy;
- the selected policy is malformed or uses an unsupported duration;
- the source offering class cannot support the selected proof flow;
- an explicit snapshot-to-official path has no selected policy; or
- production offering, definition binding, and selected policy disagree.

Runtime publication fails closed when source-final evidence names the wrong signal,
retained stabilization history is missing or unreadable, observation identities conflict,
or the producer clock would make `finalized_at` later than `created_at`. Temporary source
unavailability continues through the existing retry and escalation path.

Legacy retained obligations without a selected policy remain readable and projectable.
They are not reinterpreted or rewritten. A settled legacy official revision stays
settled; unfinished legacy official work requires a new configuration generation with an
explicit policy.

## Tests

Add focused tests for:

- policy construction, identifier validation, duration validation, and storage round trips;
- official admission requiring a selected policy and the correct source-offering class;
- source-final success, wrong signal, early evidence, and exact emitted policy fields;
- contractual cutoff refusing early dispatch and promoting the first qualifying verified
  snapshot;
- stabilization across unchanged observations without duplicate snapshot revisions;
- stabilization reset after content change or missed observation deadline;
- restart recovery from retained memory and PostgreSQL observation history;
- unreadable or inconsistent retained evidence failing closed;
- exact `finalized_at <= created_at` behavior;
- unchanged snapshot-only reporting, settling, reconciliation, materialization, and status
  projections; and
- the package-grain Reliable Reporting 1.2 snapshot offering remaining snapshot-only.

Verification will run targeted unit and conformance suites first, followed by the full
Python test, type-check, lint, and package-storage suites used by CI.
