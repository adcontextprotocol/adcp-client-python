# Reporting caller ownership upgrade

This release changes the reporting storage and public Python API contract.
A generation is identified by `(account_id, consumer_id, delivery_config_id,
delivery_config_version)`. `consumer_id` is the transport-authenticated caller,
resolved by trusted application authorization. A request body cannot authorize
an owner. Buyers sharing an account can independently use `daily@1`.

Ship this as a breaking SDK/storage release. Required configuration, obligation,
generation-key and lease constructors now include `consumer_id`. Buyer-facing
`list_configurations`, `open_snapshot` and `read_status_snapshot` require a typed
`caller`. Source bindings, obligation identifiers, reconciliation identities,
notification scopes and canonical digests include the owned generation. Update
custom stores, resolvers, source binding implementations and retained Python
fixtures together. No default or reserved sentinel principal supplies ownership.
Core status IDs, supersession chains and journal entries are scoped by account
and caller. Repeated status IDs from independent callers cannot suppress changes
or advance another caller's snapshot or checkpoint boundary.

## Maintenance order

Mixed-version reporting processes are unsupported. This includes old Core
workers and readers, managed workers, notification expansion/delivery workers,
admission endpoints, cron jobs and tools that access reporting tables.

1. Back up the database and immutable external artifacts. Inventory pending
   effects and retain original signing/destination records for reconciliation.
2. Stop all reporting admission, reads and workers. Confirm no reporting
   transactions remain. Use a dedicated operator connection and an explicit
   maintenance window; the SDK does not perform this upgrade on startup.
3. Run `migrate_legacy_reporting` with a new quarantine schema and the explicit
   `workers_stopped=True` assertion. It takes the schema advisory lock and locks
   all retained reporting tables, moves tables/sequences/functions without
   modifying their rows, installs a read-only archive guard, and creates the
   complete owned schema in the same transaction. Failure rolls back both.
4. Review authoritative ownership mapping outside buyer requests. For a known
   generation, obtain `legacy_generation_digest` and approve a
   `ReportingOwnershipBackfill` referencing that exact evidence and the external
   ownership register. Run `backfill_legacy_reporting` while still stopped.
   The mapping is immutable and idempotent. A digest mismatch, missing generation
   or conflicting mapping aborts the transaction.
5. Deploy the new SDK consistently across every reporting process. Validate
   packaged SQL and required schema contracts, rebuild caller-aware application
   bindings, and restart pagination without old cursors/checkpoints.
6. Reconcile uncertain effects manually, using the retained artifacts and actual
   destination evidence. Create a **new configuration generation** with freshly
   authenticated ownership, destination and signing admission for subsequent
   work. Start new workers only after the recovery plan has been approved by the
   operator responsible for the external effects.

`create_schema()` refuses an unmigrated legacy configuration table. Migration
and backfill APIs are operator APIs; never mount them as buyer handlers. Restrict
quarantine schema/table/function privileges to the maintenance role. PostgreSQL
owners/superusers can override guards; the application role must not own the
archive or have access to it. Do not grant application users schema creation or
migration privileges.

## Retained state and recovery

Unknown-owner generations and all inherited pending work remain in quarantine.
They cannot be read through buyer services, leased, admitted, expanded or sent.
A real authenticated caller named `__legacy__`, `unassigned` or any other
sentinel-like value receives no ownership of these rows.

A validated mapping copies only Core configurations, obligations, revisions,
rows, adjustments and their ledger change records into caller-owned read paths.
SQL copies preserve immutable columns and identifiers directly; no canonical
artifact is parsed and relabelled. Configuration lease metadata is cleared in
the imported copy and `quarantined=True` makes the generation read-only and
unleasable. The original archive remains byte-for-byte unchanged.

A trustworthy owner mapping does not supply missing reporting evidence. Older
schemas such as beta.15 can lack columns required by the retained Core model;
backfill then rejects the import with `legacy schema requires explicit
reconciliation before import`. The transaction imports nothing and preserves
the archive. Keep those records quarantined while reconciling their evidence;
use a fresh admitted generation for new reporting. Do not fill missing evidence
with request values or defaults, edit archived artifacts, or replay inherited
work. Matching retained-schema records can use the validated backfill above.

Consumer statements, reconciliation documents, frozen snapshots, materializer
work, notification queues, receipt intents, production admission, source progress
and destination/signing bindings stay in the archive. Their old canonical
identities are not trustworthy under the new caller key. Backfill never rewrites
or resigns them and never automatically replays them. Its only supported pending
work disposition is `retain_without_replay`.

For an unknown owner, leave the generation unassigned until an authoritative
mapping exists. For an unknown external effect, inspect the destination under
its original binding and deduplicate against the original stable identifier.
Record accepted/rejected/unknown outcomes in the operator's reconciliation
register. New delivery must have a fresh generation and verified destination,
caller and signing scope; an uncertain old effect is not authority to redeliver.
External artifacts retain their original bytes, names and retention obligations.

All legacy pagination positions are invalid after this upgrade. Status cursors
and incremental checkpoints return `INVALID_CHECKPOINT`, recovery `correctable`,
with a restart-pagination instruction. Archived snapshot positions cannot be
resolved by new feed readers. A buyer must restart the walk and deduplicate
immutable record identifiers it has already consumed.

## Operator example

```python
from adcp.reporting.migration import (
    ReportingOwnershipBackfill, backfill_legacy_reporting,
    legacy_generation_digest, migrate_legacy_reporting,
)

archive = "adcp_reporting_quarantine_release_ownership"
await migrate_legacy_reporting(
    operator_connection, archive_schema=archive, workers_stopped=True,
)
# Ownership register reviewed independently of any buyer request.
digest = await legacy_generation_digest(
    operator_connection, archive_schema=archive, account_id="advertiser-42",
    delivery_config_id="daily", delivery_config_version=1,
)
await backfill_legacy_reporting(
    operator_connection, archive_schema=archive, workers_stopped=True,
    mapping=ReportingOwnershipBackfill(
        account_id="advertiser-42", delivery_config_id="daily",
        delivery_config_version=1, consumer_id="buyer-a",
        authority_reference="ownership-register-2026-entry-42",
        evidence_sha256=digest,
        pending_work_disposition="retain_without_replay",
    ),
)
```

The SDK asserts the declared maintenance precondition; it cannot stop adopter
processes or establish external ownership evidence. These functions operate only
on the explicitly supplied connection. They do not migrate a production database
as part of SDK installation or normal service startup.

## Conformance release boundary

CI keeps the existing reporting gate identifiers, but replaces the previous
eight-artifact receipt rolling matrix and live feed/production rolling checks
with stopped-worker ownership, retained-state and installed-package checks.
Those historical rolling modules and their evidence runner describe the
pre-ownership releases; they are not a compatibility promise for this storage
upgrade. Current-worker transaction, crash, lease recovery and delivery tests
continue to run. Actual reviewed A/B/C binaries also verify startup refusal and
retention after a maintenance upgrade.
