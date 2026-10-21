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

## Retained source acquisitions

New source requests and manifests bind `identity.consumer_id` to the authenticated
obligation owner. Retained manifests without this optional field still decode and
keep their original canonical hashes. An active owned obligation cannot resume
an ownerless or differently owned frozen acquisition: the producer fails closed
before dispatching source work.

Inventory these acquisitions during maintenance, including state retained by
early SDK 9 beta deployments. Reconcile uncertain external effects using the
original evidence, then create a new configuration generation for subsequent
work. Do not insert an inferred owner into an old frozen request or relabel its
manifest under the same execution key. Decoding legacy evidence does not grant
permission to reuse it for an authenticated caller.

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

## An entirely empty ledger

If reporting was enabled but no configuration or other reporting state was ever
admitted, use the explicit operator API without creating a quarantine archive:

```python
from adcp.reporting.migration import replace_empty_legacy_reporting

await replace_empty_legacy_reporting(operator_connection)
await store.create_schema()
```

The function takes the schema advisory lock and `ACCESS EXCLUSIVE` locks on
every retained `reporting_*` and `adcp_reporting_*` table in one transaction.
It then refuses any populated table. If all are empty, it removes the legacy
tables, sequences and functions; external dependencies cause a rollback. Nothing
is archived or backfilled. Use a READ COMMITTED operator transaction; snapshot
isolation and active row-level security are refused because they can hide rows.
Keep reporting admission disabled until every process
uses the owned API. The table locks provide database quiescence for the empty
replacement; they do not make mixed-version reporting safe afterward.

With no transaction open on the connection, the async function commits its own
transaction, whether or not the connection is in autocommit mode. Inside a
transaction already open on the connection, it runs as a savepoint of that
transaction and commits with it.

### From an Alembic revision

`replace_empty_legacy_reporting_sync` is the same replacement for synchronous
migration frameworks. Pass it Alembic's `op.get_bind()`: a SQLAlchemy
`Connection` on psycopg2 (the default `postgresql://` driver) or psycopg 3
(`postgresql+psycopg://`). A raw sync psycopg 3 or psycopg2 connection also
works. Unless the connection is in autocommit mode with no transaction open,
the replacement joins the caller's transaction and never commits it. The drop
therefore commits atomically
with the revision's version bump. If anything later in the run fails, both roll
back. It never starts an event loop, so it is safe when the migration runner is
itself called from async code.

```python
"""Replace the empty pre-ownership reporting ledger."""

import sqlalchemy as sa
from alembic import op

from adcp.reporting.migration import (
    replace_empty_legacy_reporting_sync,
    replace_empty_owned_reporting_sync,
)


def _ledger_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("reporting_configurations"):
        return set()
    return {column["name"] for column in inspector.get_columns("reporting_configurations")}


def upgrade() -> None:
    columns = _ledger_columns()
    if columns and "consumer_id" not in columns:  # an 8.x ledger
        replace_empty_legacy_reporting_sync(op.get_bind())


def downgrade() -> None:
    if "consumer_id" in _ledger_columns():  # see "Rolling back to an 8.x image"
        replace_empty_owned_reporting_sync(op.get_bind())
```

The shape check only makes the revision a no-op on a fresh database. The helpers
re-verify the shape under the schema lock and refuse anything unexpected. The
table locks are held until the migration transaction commits. To fail fast
rather than wait behind running queries, run `SET LOCAL lock_timeout = '15s'`
before the call. Do not call `create_schema()` in the same revision. It uses
another connection, which waits for the schema lock this transaction holds while
the revision waits for it. PostgreSQL cannot detect that deadlock, because it
spans two client connections, so the migration hangs. The owned schema is
created when the service starts, as usual. A refusal rolls back
only the helper's own savepoint, and the exception then fails the revision.
Offline (`--sql`) mode has no connection and is not supported.

## Rolling back to an 8.x image

An 8.x image cannot run against the caller-owned ledger. Its startup keeps the
existing owned tables, and its INSERTs omit `consumer_id`. Before deploying an
8.x image again:

1. Stop every SDK 9 reporting process, including admission, readers and workers.
   Keep them stopped until the 8.x image is deployed.
2. If the owned ledger never admitted any reporting state, remove it with
   `replace_empty_owned_reporting(operator_connection)`. In an Alembic
   `downgrade()`, use `replace_empty_owned_reporting_sync(op.get_bind())` as
   shown above. Both give the same guarantees as the replacement: READ COMMITTED
   isolation, the schema advisory lock, and `ACCESS EXCLUSIVE` on every
   `reporting_*` and `adcp_reporting_*` table before any row is checked. They
   then remove those tables, sequences and functions. Rows that installation and
   startup write do not count as state: the change-feed horizon before its first
   prune, and registered row-storage bindings. `adcp_persistence_installation`,
   the row-storage installation identity, is kept. 8.x ignores it, and a later
   upgrade reuses it.
3. Deploy the 8.x image. It creates its own ledger on start. To upgrade again
   later, follow [An entirely empty ledger](#an-entirely-empty-ledger).

The removal refuses, and changes nothing, when any reporting table holds another
row, when active row security could hide rows, or when objects outside the
reporting surface depend on it. An 8.x image cannot represent owned rows, so
there is no supported downgrade of a populated ledger. Keep running SDK 9, or
restore the backup taken before the upgrade, which loses everything written
since. A ledger created by `migrate_legacy_reporting` is refused with a specific
error, because the archive registry names its quarantine schema. The legacy rows
live in that archive, and removing the owned ledger would not restore them.
Restore the pre-upgrade backup instead. Do not move archived tables back by
hand: they carry the quarantine triggers and the maintenance role's ownership.

## Rolling deployment boundary

The owned configuration and obligation tables require `consumer_id NOT NULL`.
An 8.x INSERT that omits the owner fails instead of silently creating an unowned
row. This is a bounded failure for that INSERT, not a guarantee for every old
operation: old reads, UPDATEs and DELETEs can still access another caller's rows
because they do not filter by owner.

For the empty case, disable reporting admission, replace the empty schema, deploy
all reporting processes with SDK 9, create the owned schema and then enable
reporting. Do not admit owned state while old reporting processes remain active.
For a populated ledger, use the stopped-worker maintenance order above.

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
