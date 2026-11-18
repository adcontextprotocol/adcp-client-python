# Provisional observations and upgrade status

This caller-ownership release requires all reporting readers, admission and workers to stop during upgrade. Earlier additive or rolling procedures below apply only to pre-ownership releases; use the [maintenance and recovery contract](reporting-caller-ownership-migration.md) for retained state.

The producer records every successful scheduled provisional read as a durable
observation. Changed content creates an immutable revision; unchanged content
retains the current snapshot and advances the checkpoint without a notification. A durable
reservation freezes the acquisition before source work starts. Its successful
observation and next checkpoint commit together with any revision rows, ledger
change and notification. A retry of the same reserved acquisition returns the
existing observation instead of appending another one.

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
- Setting `official_close_lag` opts a snapshot offering into an explicit
  snapshot-to-official route. This requires a declared `restatement_window`
  and a selected definition policy. For `source_final` and
  `contractual_cutoff`, the lag can delay the source attempt. For
  `stabilized`, the SDK promotes the snapshot when its scheduled observations
  satisfy the selected policy, independently of the lag value.

The durable acquisition freezes the source request, predecessor revision and
observation ordinal. A retry renews only `deadline_at`, the execution budget for
that attempt. It retains the execution key, run ID, cutoff, scope and remaining
request fields even after the original deadline expires. Once reserved, a
successful retry completes that observation before a later acquisition starts.

## Selecting official finality

For a new generation with `required_finality: official`, the adopter copies
one policy from the pinned report definition into
`ReportingDefinitionBinding.finality_policy`. Use the same selection when
configuring an explicit snapshot-to-official close. To migrate unfinished
official work with no policy, configure a new generation with the selected
policy. Retained revisions and exact replays keep their original evidence.

- A `source_final` policy uses the authoritative offering. Its
  `provider_declared` or `provider_job_terminal` evidence must carry the exact
  selected signal and the upstream finalization time.
- A `contractual_cutoff` policy uses the snapshot offering. Its full, verified
  observation must reach the declared period-end cutoff.
- A `stabilized` policy uses the snapshot offering and durable scheduled
  observation history. Changed content or a missed cadence restarts the
  unchanged interval. Manual reads do not establish a run.

`stable_since` is a source observation and `official_close_lag` is scheduling
metadata. Snapshot-only generations need no selection and remain snapshots
regardless of elapsed time. After an official close the producer accepts exact
replay. Later corrections use adjustments.

Custom Core ledgers that publish official revisions must implement
`ReportingSourcePublicationStore.source_publication(account_id)` with a lock
shared across all publishers for that account. Stabilization additionally
requires durable reservations, bounded scheduled history, checkpoints, exact
snapshot origins and generation schedule recovery. An ordinary snapshot route
can keep using its snapshot publisher when the definition also lists an unused
stabilization policy.

## Persistence and compatibility

`PgReportingLedgerStore.create_schema()` installs two additive private tables:
`reporting_provisional_acquisitions` and `reporting_provisional_observations`.
They retain immutable reservation and observation metadata. The existing
restatement checkpoint remains the progress cursor. The in-memory store has the
same atomic operations. Custom producer stores must implement
`ProvisionalObservationStore` from `adcp.reporting.ledger.provisional` together
with `RestatementCheckpointStore`.

The packaged `reporting/ledger/required_provisional_schema.json` describes the
required objects. Bootstrap and each reservation, observation
read, and atomic observation commit validate those objects, including immutable
triggers and their function definitions. Validation also runs with notifications
disabled. Missing, disabled or changed objects fail closed with
`PROVISIONAL_SCHEMA_UNREADY`; reinstall or repair the schema before resuming
production. Notification readiness alone does not establish observation readiness:
historical notification-only schemas remain valid for their original operations.

Existing revision tables and exact-read row layout are unchanged. An unchanged
observation points to the retained snapshot revision. The observation table's
revision ID uniqueness constraint is removed by `create_schema()` so multiple
observations can reference the same immutable revision. Historical readers can
still read rows directly from `reporting_revision_rows` for every revision.

This change retains per-revision row payloads. Content-addressed reuse of staged
source objects is a separate pending change; complete payload deduplication has
not been accepted. Do not interpret immutable observation support as completion
of the full no-duplicate-payload requirement.

Stop old producer writers before switching scheduling behavior. Old binaries do
not write the new observation metadata, even though they can still read the
existing revision rows. Historical-binary rolling tests and full service
qualification must pass before claiming an upgrade is supported.

## Custom stores and publication decorators

Declare the checkpoint and observation methods explicitly on the store class.
Dynamic `__getattr__` forwarding does not enable these optional capabilities.
The producer checks declarations consistently across supported Python versions;
an incomplete adapter raises `RESTATEMENT_CHECKPOINTS_NOT_SUPPORTED` or
`PROVISIONAL_OBSERVATIONS_NOT_SUPPORTED` before source acquisition. It does not
fall back to separate revision and checkpoint writes.

A decorator that prepares revisions in `commit_revision` must also prepare them
in `commit_provisional_observation`. Explicitly compose all three observation
methods (`reserve_provisional_acquisition`, `get_provisional_observation`,
`commit_provisional_observation`) and both checkpoint methods
(`get_restatement_checkpoint`, `record_restatement_checkpoint`). Reuse the same
revision preparation, validation and fault boundaries around the delegated atomic
commit. Calling ordinary `commit_revision` first would break the observation's
transaction boundary; forwarding the new method without preparation would bypass
the decorator's publication rules.

An `OBSERVATION_CONFLICT` fails only the affected slice for that worker turn. Its
pending work remains eligible for a later retry while sibling slices continue.
Schema or capability failures stop the turn so the operator can repair the store.

## Pending scheduling qualification

The production progress queue still selects bounded pending work before checking
its observation due time. During the new provisional window, future-due work can
therefore consume turn capacity that would otherwise acquire newly closed periods.
Durable due-time selection and its fairness tests remain a service activation
requirement; the default window is not a claim of completed production rollout.

Successful observations retain their next cadence checkpoint. Failed or not-ready
attempts retain their acquisition identity and a durable retry deadline. Worker
turns and restarts respect that deadline instead of retrying on every poll. A
retry retains its frozen cutoff, execution key and request fields; only its
attempt deadline is renewed. This retry timing does not resolve the progress
queue fairness requirement above.

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
`(account_id, consumer_id, delivery_config_id, delivery_config_version, period_end,
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
