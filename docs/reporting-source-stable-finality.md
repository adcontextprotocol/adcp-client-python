# Source stable package finality

ESA's paid media package definition requires a 90 day source requery window,
content change publication, and official close only after one day of unchanged
source data at or beyond the 90 day age. Configure the SDK's provisional
snapshot offering with `restatement_window="P90D"`, a source safe
`restatement_cadence`, `official_close_lag="P90D"`, and
`official_stability_duration="P1D"`. Keep the provisional offering and add a
distinct `AuthoritativeOfferingV1` for the official read. Set its
`correction_policy="immutable_correction"` and choose a correction window that
matches the definition. Wire both offering IDs into `ProducerOfferings`.

The snapshot adapter should return the complete package rows and control totals
on every scheduled requery, including an explicit zero-row result when the
package has no activity. An unchanged read advances the durable observation,
checkpoint and next due time; it does not create a revision, ledger change or
notification. A changed read publishes a snapshot that supersedes the current
snapshot. The final snapshot recheck at the 90 day boundary is inclusive.

For an official read, the source adapter must return `InlineFetchResult` with
`finality_evidence_ref` identifying authoritative source evidence and
`finality_stable_since` naming when that source content last changed. The
producer requires both, checks that the source has been stable for the declared
duration, and waits for the period age, provisional boundary and last changed
snapshot's stability duration. A provisional observation alone never establishes
official finality. An official attempt with missing or premature evidence records
a nonpublishing checkpoint, so its retry uses a fresh source execution key and
read cutoff. The official offering's readiness fields should reflect when
the authoritative source can answer. An official correction after close must
be published through the existing adjustment flow, not as another snapshot or
replacement official revision.

ESA should adopt the first SDK release containing this change, expected to be
`adcp==10.0.0b21` after its release. The PostgreSQL producer also needs the
matching `adcp-reporting-storage` release containing the observation constraint
migration. Do not deploy the SDK change with an earlier storage package.
