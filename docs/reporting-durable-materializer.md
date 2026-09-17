# Durable materializer — #1167B2.1

**B2.1 of 4 within B2 of B1/B2. Refs #1167.** This unit builds on the
independently approved B1 commit
`1c91311ec28d25506d5db43f59d0c34936ecb8f7`. It supplies durable reservation,
verified destination I/O, recovery, and one atomic finish transaction. It does
not activate a complete Managed Delivery or Reconciled Billing offering.

The remaining, separately reviewed dependencies are B2.2 (seller revision and
adjustment receipt ingress), B2.3 (persisted combined feed and revision
ownership), and B2.4 (versioned reconciliation projection, offering readiness,
and production notification activation). They remain required inside B2.
Full Python buyer adjustment automation and `client.reporting` remain the
explicit Wave 6 prerequisite owned by the coordinator. The later #1172
cross-language process matrix is separate.

## Composition

Use the [strict production-oriented example](../examples/reporting_durable_materializer.py).
Construct `PgReportingMaterializerStore(pool=pool, notifications=False)` for an
explicit polling-only deployment, or set `notifications=True` for the atomic
notification path. Install schema during deployment, then call
`compose_materializer` with the application's installed verifier registry and
trusted resolver/writer. Run `service.run_once()` repeatedly; the adopter never
enumerates accounts. The optional HTTP delivery worker is not a materializer
dependency.

`materializer_ready()` validates storage prerequisites; it is **not** a tier or
mount readiness certificate. There is no caller-supplied production-ready
switch. B1's development writer remains ineligible, and the current capability
helpers retain C's Managed/Reconciled veto. This unit offers a production
orchestration primitive, not an activated seller offering. Core-only deployments
keep their existing stores and do not need destination or receipt components.

Import the optional `ReportingMaterializerStore` protocol, service, lease,
turn and boundary types from `adcp.reporting.materializer`. The PostgreSQL store
is lazy; importing the public surface without `[pg]` works. Older required
store protocols, constructors, enums, and closed record decoders are unchanged.

## Transactions and discovery

`reporting_materializer.sql` installs only objects named `reporting_materializer_*`:
account scheduling heads, binding discovery cursors, obligation candidates,
work, captured status heads/boundaries, and isolated notification events and
expansion work. Its own `materializer/required_schema.json` validates the exact
objects. No B2 object is appended to A, B, C, or B1's mandatory manifest, and no
old guard is replaced. Old, partial, and structurally mismatched installations
fail closed. The migration is transactional, repeatable and serialized per
schema; an interrupted installation leaves no partial work schema.

Installation seeds retained destination bindings once. A durable keyset cursor
discovers at most 32 existing obligations per turn. New binding and obligation
triggers cover either arrival order; restart never rewinds a completed cursor.
Publication, restatement, readability, configuration and materialization-check
mutations dirty indexed candidates. A consumer receipt does not schedule a
materializer retry. Exact no-op writes do not invalidate a generation.

Claims read a bounded page of at most 16 due accounts without row locks, try the
account advisory lock, and only then lock candidate/work rows in that account.
There is no global `SKIP LOCKED` followed by an account lock. A bounded sampling
cursor advances past busy account pages and wraps; persistent served positions
keep other accounts eligible across restarts. Workers do not periodically scan
the complete ledger. PostgreSQL time controls due dates and 3–300 second leases;
tokens are random UUIDs. Account advisory locks are shared across schemas in
one database, so concurrent test groups must use different databases or run
sequentially when fixture account IDs collide.

Reservation creates any missing immutable obligation delivery record, the next
immutable attempt, and its durable work on the same connection. Binding may
arrive after publication. Its retention floor is the later of reservation time
and period end, plus the binding's retention duration. An inactive or
deactivated configuration parks work; a future activation is scheduled, and
configuration changes wake the existing history. A binding itself is immutable;
replacement uses a new configuration generation, not an in-place destination
rewrite. Exact trusted principal/binding authorization is still checked at I/O.

Source preparation, destination write, and verifier-controlled paginated
readback occur **outside** the final account/work transaction. A service-owned,
cancellation-safe heartbeat renews the lease while I/O runs, including with a
size-one connection pool. Each I/O phase has a deadline and joins cleanup. The
service reselects authority before preparation, immediately before write, and
before readback. Write and readback open separate freshly authorized credential
sessions. Preserve the SDK session manager and put every credential and partial
acquisition inside its redacted, non-persistable context; close happens exactly
once, including interrupted opens.

Finish takes the account lock on one connection, checks exact schema, the
unexpired token, frozen generation/binding/request, SDK-sealed verification, and
strict current/readable revision selection from complete history. Only the SDK
readback verifier can mint the process-local immutable verification seal;
it is bound to that reservation's fencing token and revalidated under the
finish lock. Restart or a new fencing token requires a fresh readback. Private
provenance stays outside B1's three-field constructor, dataclass serialization
and Pydantic wire shape. The transaction commits all of:

1. The immutable success or compatible safe failure.
2. Required old status dirty work and an isolated immutable boundary containing
   actual captured Core and caller-private reconciliation inputs, DB `as_of`,
   and monotonic caller/account sequence heads.
3. The logical readiness enqueue when enabled and this is a new verified success.
4. The fenced work ACK and next candidate schedule.

There is no reacquired connection or external I/O in that unit. An enqueue,
capture, fence, or commit failure rolls it all back. Exact completed replay
does not enqueue, capture, or allocate again. The memory conformance store
unconditionally restores all changed collections and initialized sequence
heads on failure, with notifications either off or on.

## Notification activation boundary

The finish uses the existing SDK logical event builder, identity and enqueue
implementation through a closed table adapter on that same connection. The
queue retains caller namespace, deterministic logical cause/idempotency, and
account-ordered captured provenance. Its expansion-work row points to the
logical event; recipient selection/fanout is the separately crash-safe phase
from #1168A, not a synchronous finish requirement.

**Every B2.1 reservation has immutable `admission_epoch=0`. Every event from
that work is permanently quarantined.** SQL rejects promoting its expansion
work or changing the event/work provenance. No legacy worker reads these
tables, and B2.1 installs no materializer HTTP dispatch route. Creating this
quarantined internal intent does not advertise or emit `reporting.delivery_ready`.
Failures, stale work, corruption, exact replay, and Core mode enqueue no intent.
Explicitly disabled notifications enqueue nothing. Enabled work cannot be
resumed by a replacement process configured with notifications disabled.

B2.4 must extend the single SDK composition with real installed component and
projection readiness, an isolated activation/fence, and a compatible read path.
Only a **newly admitted reservation** after that activation can enqueue a
deliverable logical event, in its original verified-finish transaction. No
later best-effort copy/publish transaction may become the only actual enqueue.
Pending epoch-zero work retains its epoch and external identity after restart
or upgrade, even if it finishes after activation. Completed epoch-zero events
are never released, promoted, copied, or replayed as current readiness.

This policy avoids historical delivery under a changed offering, principal,
binding, configuration generation, expired/revoked evidence, or newly selected
official revision. It also avoids inventing retries for previously successful
work just to generate an event. Captured records remain available for polling
and B2.4's explicit baseline/activation procedure. B2.1's minimal capture
primitive preserves the original finish boundary; it does not replace C's
projector or reconstruct historical boundaries from today's state. Full
versioned projection, drain/fence of incompatible C workers, and delivery
activation belong to B2.4.

## Recovery and retention

| Durable situation | Autonomous behavior | Operator action |
| --- | --- | --- |
| Pending; timeout, cancellation, worker/connection/lease loss, or unknown external effect | Resume the same attempt and external identity after the lease/backoff | Repair availability; do not reset the sequence |
| Own known terminal failure allowing a new attempt | Allocate N+1 only after that failure; never while N is pending | None for a retryable failure |
| Own terminal failure with no retry | Park as `operator_required` | Correct the cause and explicitly recover through supported persistence |
| Unowned legacy pending attempt | Park as `legacy_pending`, including when a later official revision exists | Drain legacy writers and prove/import the original external identity |
| Legacy terminal outcome | Preserve it; do not silently own its retry policy | Explicit recovery; ordinary public N+1 persistence remains supported |
| Current revision/readability changes during I/O | Immutable safe failure, no readiness and no artifact reuse | A new selected revision uses its own existing history; same-revision recovery increments that history |
| Fork, cycle, multiple official leaves, broken attempt history | Park as `history_corrupt`; no hot loop | Repair/import under an audited operator procedure |
| Successful artifact later becomes unreadable, revoked, unhealthy or expired | Preserve the immutable successful outcome/count; park or project degraded health | Repair authority/health or explicitly persist recovery; no automatic new attempt after success |

Public persistence still permits attempt N+1 after **any** immutable terminal
outcome, and an ordinary public materialization outcome keeps producing its usual
status projection work. Only the deliverable readiness event is reserved for the
fenced verified finish; the projector must never go stale because an adopter
persisted an outcome itself. The autonomous allocator intentionally owns a
narrower retry policy.
Attempt numbers are revision-specific. Selecting a different revision never
deletes another revision's work/history; selecting the same unreadable revision
never resets its sequence. Official-required selection never falls back to a
snapshot when official evidence is missing or unreadable. Rich internal reasons
map to the existing public `MaterializationFailure` variants; the public enum
is unchanged.

Before enabling this service, **drain every legacy materialization writer**.
For a known compatible pending effect, call
`import_pending_materialization(scope=..., reporting_materialization_id=...,
original_external_id=..., keys=...)` only after independently verifying its
original destination identity. The primitive requires the exact SDK identity,
current immutable binding/key and sole pending history. It cannot infer an
unknown or different legacy provider identity. Unknown legacy effects require
operator resolution and an explicit terminal record; do not manufacture proof
or have the service adopt them automatically. Restore the same installed key
and explicit import to wake parked owned pending work without changing its ID.

The destination must durably enforce its advertised idempotent/conditional
write identity. No database transaction can eliminate the external-write /
outcome-commit crash window. Resume must not overwrite a different artifact.
The verifier rechecks all pages, exact digests, totals, rows, immutable locator,
retention and principal. Finish checks retention again against DB time.

Keep attempts, outcomes, bindings, work, captured inputs and event provenance
conservatively; this unit installs no automatic garbage collector. Never purge
pending work or the external identity needed to recover it. External cleanup
is best effort and cannot authorize success, a retry, or an ACK. A cleanup
failure must not change correctness. Observe only closed `ReportingMaterializerTurn`
state/reason and opaque IDs. Never log sessions, credentials, signed URLs,
provider bodies, raw driver exceptions, or secret destination configuration.

An out-of-range `lease_seconds` or boundary position is a caller-argument
rejection, not a destination outcome: it raises a `ValueError` naming the bound
before any store or destination work. Reserve `ReportingWriterError` for real
failures, where `retry`/`effect` describe an actual external attempt.

## Rolling compatibility envelope

The executable gate is
`tests/conformance/reporting/test_reporting_materializer_rolling.py`. It builds
and installs actual wheels from these exact commits, verifies installed module
origins and source SHA-256 hashes, and runs them with `python -I` outside the
checkout. It first installs each native schema, then the **actual approved B1
wheel** as comparison baseline. The same frozen binary reads populated records
and performs permitted ordinary writes both before and after B2 installation.

| Frozen binary | Exact commit | Notification readiness on C/B1 baseline and after B2 |
| --- | --- | --- |
| beta.15 | `3e76aa54623529a3dda01cd690b8a5c287c75641` | No notification API |
| #1167A records | `3c405a21f978ed9d3208611bb4a7a8434a056933` | No notification API |
| Foundation integration | `037de4ac822ecefb2f95d32c15c297fb4c45d683` | No notification API |
| #1168A outbox | `21bf443e7d850d1800ec8a6f2e4abec1c8f85541` | Aggregate readiness closed on both |
| #1168B activity | `198d50e61c74fb82aedbf2c77e06a0e200b91db6` | Required-object readiness valid on both |
| #1168C status | `ea150fabd5ad90e3abf93f89729d2919f1c61798` | Required-object readiness valid on both |
| #1167B1 | `1c91311ec28d25506d5db43f59d0c34936ecb8f7` | Required-object readiness valid on both |

The historical positive-readiness fixture requires PostgreSQL 16, **UTF8 with
`LC_COLLATE=C` and `LC_CTYPE=C`**. Frozen A is ready on its native schema under
that precondition. Reviewed C's additive triggers already close A's aggregate
digest on the approved C/B1 baseline. B2 must preserve that exact classification;
it is not a new waiver. A's permitted default-off Core reads/writes and existing
ordinary notification worker remain functional. The newer B/C required-object
manifests stay ready. The gate checks old catalog objects unchanged and that
every newly introduced object has the isolated prefix.

Frozen beta.15 retains its original definition shape; monetary-unit declarations
are an A prerequisite. URL reconciliation principals became readable in C.
Earlier binary probes use their supported opaque principal with a second URL
consumer populated in the same account; C/B1 and current probes exercise URL
principals directly. This preserves the actual historical contract rather than
loosening an installed decoder assertion. Old readers do not see another
caller's materializations. Actual old ordinary/status workers consume positive
control events but cannot claim/mutate B2 event/expansion work, materializer work,
or captured boundaries/heads.

Non-C historical aggregate portability is a separate probe with a different
precondition; it must not be presented as this positive readiness gate or as
shared-database lock contention. New B2 failures on supported old operations
are regressions, regardless of the inherited frozen-A aggregate limitation.

Run the historical comparison with its provenance output preserved:

```sh
ADCP_PG_TEST_URL="$REPORTING_TEST_DSN" python -m pytest -q -s \
  tests/conformance/reporting/test_reporting_materializer_rolling.py
```

Run the materializer PostgreSQL groups sequentially per database. The focused
shared vectors, process kills, migrations and installed-artifact gate are in
`test_reporting_materializer_{durable,transactions,history,service,process,migration,installed_pg}.py`.
Set `ADCP_PYTHON310` to a real 3.10 interpreter to run VCS and sdist-built wheels
on that interpreter; base wheel tests explicitly prohibit PostgreSQL extras.
CI includes the B1 stacked target, all Python 3.10–3.13 jobs, existing PostgreSQL
and status jobs, and a bounded dedicated UTF8/C materializer artifact job. See
the PR's exact head evidence for commands, pass/skip counts and module origins;
a skipped or absent job is not passing evidence.

Artifact tests retain exact commit, wheel/module hashes, installed module
origins, database preconditions, and event identities in their output. Frozen
builds use compressed archives, remove extracted build-only sources after
hashing, and clean their own installed trees after dependent processes finish.
The common wheel/sdist fixture likewise removes its owned build/install tree
at teardown. Its optional `ADCP_REPORTING_DISTRIBUTION` cache is reusable only
when the complete source fingerprint matches; a changed input fails instead
of silently using an old wheel. A build failure reports a bounded allowlist of
stderr signatures and its stage/exit/digest, never arbitrary provider prose.

For repeated local matrices, preserve evidence logs outside disposable fixture
trees, run artifact-heavy groups sequentially, and budget for the repository's
2.2 GiB schema cache while a build is active. Clear completed task-owned pytest
scratch and obsolete local `adcp` package cache entries only after checking
process ownership. Recreate contaminated temporary databases with the same
UTF8/C precondition. Disk-full or source-straddling runs are exploratory and
cannot certify the frozen candidate.
