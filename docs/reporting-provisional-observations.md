# Provisional observations and upgrade status

The producer records every successful scheduled provisional read as a new
immutable revision, including a read whose rows are unchanged. A durable
reservation freezes the acquisition before source work starts. Its successful
observation, revision rows, ledger change, notifications and next checkpoint
commit together. A retry of the same reserved acquisition returns the existing
revision instead of appending another observation.

This is a producer and persistence change. Complete service rollout qualification
is still pending, including activation of previously retired work, source-object
reuse, installed-package checks and independent integration review. Follow the
[production guide](reporting-production.md) and
[migration guide](reporting-ledger-migration.md) for the surrounding deployment gates.

## Scheduling

- An offering without `restatement_window` uses an SDK fallback of 72 elapsed
  hours after the reporting period ends. An explicit offering window continues
  to apply.
- The first reserved acquisition freezes the window and cadence. Cadence is the
  explicit `restatement_cadence`, or the greater of the reporting-period duration
  and `fastest_safe_cadence`. Later successful reads retain that policy.
- A successful read anchors the next due time at its checked/acquired instant
  plus cadence, capped at the resolved provisional boundary. After downtime,
  one due read runs; missed intervals do not create a burst of catch-up reads.
- The adapter may return typed `InlineFetchResult.provisional_until` evidence to
  shorten or extend the boundary. It must be timezone-aware and at least the
  source observation time. Invalid evidence commits no revision or checkpoint.
  If evidence is absent, the frozen offering window or SDK fallback applies.
- Snapshot-only reporting retains one final inclusive read at the boundary,
  including after downtime. Valid new source evidence may extend that boundary.
  Expiry does not create an official revision.
- Existing explicitly configured official-close behavior remains in force.
  `official_close_lag` still requires an explicitly declared `restatement_window`
  and an official offering. The SDK fallback does not opt an adapter into
  automatic official publication.

The durable acquisition freezes the source request, predecessor revision and
observation ordinal. A retry renews only `deadline_at`, the execution budget for
that attempt. It retains the execution key, run ID, cutoff, scope and remaining
request fields even after the original deadline expires. Once reserved, a
successful retry completes that observation before a later acquisition starts.

## Persistence and compatibility

`PgReportingLedgerStore.create_schema()` installs two additive private tables:
`reporting_provisional_acquisitions` and `reporting_provisional_observations`.
They retain immutable reservation and observation metadata. The existing
restatement checkpoint remains the progress cursor. The in-memory store has the
same atomic operations. Custom producer stores must implement
`ProvisionalObservationStore` from `adcp.reporting.ledger.provisional` together
with `RestatementCheckpointStore`.

Existing revision tables, exact-read row layout and pinned migration manifests
are unchanged. Each observation has its own revision ID and revision-specific
wire content hash. Unchanged source content retains its source content identity;
it does not mean that the revision ID or wire hash is reused. Historical readers
can still read rows directly from `reporting_revision_rows` for every revision.

This change retains per-revision row payloads. Content-addressed reuse of staged
source objects is a separate pending change; complete payload deduplication has
not been accepted. Do not interpret immutable observation support as completion
of the full no-duplicate-payload requirement.

Stop old producer writers before switching scheduling behavior. Old binaries do
not write the new observation metadata, even though they can still read the
existing revision rows. Historical-binary rolling tests and full service
qualification must pass before claiming an upgrade is supported.

## Previously retired work: activation requirement remains open

A deployment that already processed a snapshot with no declared window may have
retired that obligation under the old one-shot policy. Installing this schema or
restarting a producer does **not** re-enroll it. Until the activation migration
below is implemented and verified, the new fallback applies to eligible pending
work; it is not a complete upgrade path for existing deployments.

The affected progress APIs are `ReportingProducerProgress.next_producer_obligations`,
`finish_producer_acquisition`, `commit_producer_period` and
`producer_closed_through`. PostgreSQL retains retired rows as `state='settled'`
in `reporting_production_source_work`; `reporting_production_source_progress`
retains the generation's `closed_through` and acquisition turn. The in-memory
equivalents are `_production_source_work`, `_production_closed` and
`_production_source_turns`. Selection currently considers only pending work.

Existing configuration enrollment installs generation/source/destination
bindings, without changing a retained work item's state. Repeated production
activation returns without re-enrollment. Creating a new configuration generation
would change the frozen reporting identity and is not a repair for an old
obligation. Rewinding `closed_through` is also unsupported.

The activation owner must provide an explicit disposition for each affected
deployment. A candidate bounded migration starts with an admitted account and
configuration generation, selects settled snapshot obligations in a recorded
period-end range, excludes official publications, and resumes eligible work
without changing the obligation or its retained policy evidence. It must also
define the treatment of already-expired obligations and source overrides; a
simple three-day filter alone cannot establish eligibility for every legacy
record. Missing policy evidence needs an explicit upgrade decision.

An indexed selection can use
`(account_id, delivery_config_id, delivery_config_version, period_end,
reporting_obligation_id) WHERE state='settled'`, a bounded period range and
keyset batches. The current pending-only index does not cover this selection.
Any index and re-enrollment operation belong in an additive migration, under the
existing account/generation lock, with idempotent progress and preserved
acquisition turns. Do not scan all history on each worker turn or change the
separate delivery queues to trigger acquisition.

The required regression starts with an old writer completing a no-window
snapshot, leaving its work row settled. Upgrade while the intended provisional
boundary is still open, activate/configure using the same frozen generation,
advance one cadence, and require a second immutable snapshot with the original
revision still readable. Current activation does not satisfy that regression.
The completed migration must also cover official, expired, parked, foreign-account
and interrupted/resumed batches without changing unrelated work.
