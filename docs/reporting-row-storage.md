# Reporting row storage

By default `PgReportingLedgerStore` keeps every revision row in `reporting_revision_rows`, one JSONB value
per row. At pulse cadence that keeps bulk report bytes in the transactional database, and a JSONB value does
not preserve the canonical bytes a digest was computed over.

Row storage is the shared SDK persistence layout from
[adcontextprotocol/adcp#7996](https://github.com/adcontextprotocol/adcp/pull/7996), tracked here in
[#1457](https://github.com/adcontextprotocol/adcp-client-python/issues/1457). The JavaScript SDK reads and
writes the same tables and the same bytes, so chunks written by either SDK verify in the other.

## Enable it

```python
from adcp.reporting.ledger import PgReportingLedgerStore, ReportingRowStorageOptionsV1

store = PgReportingLedgerStore(
    pool=pool,
    row_storage=ReportingRowStorageOptionsV1(deployment_namespace="prod-us"),  # or row_storage=True
)
await store.create_schema()  # applies reporting_row_storage.sql and registers the binding
```

If you manage DDL outside the SDK, apply `adcp/reporting/ledger/reporting_row_storage.sql` after the ledger
migration (PostgreSQL 13 or later) and call `await store.ready_row_storage()` on boot. `ready_row_storage` checks
the installed objects against `required_row_storage_schema.json` and refuses drift with
`STATE_UNAVAILABLE`.

Two SDKs sharing one database must use the same `deployment_namespace`; a binding registered under another
namespace is refused with `UNSAFE_BINDING`.

**A ledger database has exactly one writer SDK.** Python and JavaScript can read each other's chunks (that is what the
interop test proves), but mixed-SDK *writers* on one account are not supported. Each SDK serializes an account's commits
under its own advisory lock key, so a Python append and a JavaScript append to one account can interleave: with a reader
that orders by `seq` alone, the slower commit's lower `seq` is skipped for good. Account readers of the shared change
feed now also read only changes older than every in-flight transaction (see the feed section), which removes that skip,
but the two SDKs still keep separate obligations, revisions and tombstones, so the supported topology stays one writer
SDK per database.

To install everything this guide uses while developing: `pip install -e '.[dev,pg,reporting-gcs,reporting-s3,reporting-azure]'`
(the `pg` extra provides psycopg).

## What changes

- New revisions store their rows as canonical JSONL chunks: `JCS(row)` followed by `\n`, in 500-row segments
  and chunks of at most 10,000 rows or 8 MiB. Chunk bodies are `BYTEA` in `adcp_reporting_chunk_bodies`,
  content-addressed per obligation, and the database checks each body's digest.
- `read_revision_rows` verifies every segment it releases against digests committed in PostgreSQL, and verifies
  the chunk manifest against the row set's committed `row_manifest_sha256`. It reads only the segments a page
  needs. A missing body is `ROWS_UNAVAILABLE`, a mismatch is `ROWS_INTEGRITY_FAILED`, and neither is ever
  reported as an empty page. Rows retired by retention read like an unknown revision.
- Reads work on both forms, with or without the write option: a revision with a row set reads from its chunks, and
  one without reads from the legacy table.
- Rows must stay inside the portable number domain: finite numbers and integers within +-(2^53 - 1). Put
  larger integers in strings. Reading accepts any RFC 8785 number.

## Layout notes

`adcp_reporting_row_sets` is the side table that records where a revision's rows live until the shared Core layout
(persistence Stage 3). It matches the JavaScript table column for column except that `obligation_id` has no
foreign key: Python obligations live in `reporting_obligations`, not in the JavaScript
`adcp_reporting_obligations` table. If the JavaScript migration created the table first, `ready_row_storage`
refuses with the constraint name; apply this migration first, or drop that constraint.

A revision written before protocol-shape control totals (see the release notes) has a binding over `{name, value}`
pairs. Its row set records the rows-only digest profile `rows_v1`, and its pair binding is verified against the
stored bytes on a full read.

## Migrating legacy inline rows

Revisions written before you enabled row storage keep their rows in `reporting_revision_rows` and keep reading
through it. To move them into verified chunks, run the resumable migration in bounded batches. Each revision
migrates in its own transaction under the account lock, so a batch never holds the lock longer than one revision.

```python
cursor = None
while True:
    batch = await store.migrate_inline_rows(limit=100, cursor=cursor)
    if batch.quarantined:
        log.warning("unverifiable inline rows: %s", batch.quarantined)
    cursor = batch.cursor
    if cursor is None:
        break
```

A revision is migrated only if its inline rows, re-canonicalized, reproduce its committed
`revision_content_sha256` (a retained pair-hashed revision is proven under the algorithm it was written with), the
ordinals are contiguous and the row count matches. Anything else is **quarantined**: recorded in
`adcp_reporting_row_quarantine`, reported in `batch.quarantined`, never rewritten, and refused by every read with
`ROWS_INTEGRITY_FAILED`. Investigate it as damaged evidence; the inline rows are left exactly as found.
Inline rows are not deleted by migration.

Once a pass finishes with no cursor, stop every writer that predates chunked rows and retire the legacy table:

```python
await store.retire_legacy_inline_rows()  # ROWS_MIGRATION_INCOMPLETE while any revision is unaccounted for
```

This **renames** `reporting_revision_rows` to `adcp_reporting_legacy_revision_rows_v0`. It never truncates the
table, so a binary that predates chunked rows fails closed (its insert hits a missing table) instead of writing rows
nobody reads. `create_schema` does not recreate the retired table, and the schema checks for notifications and the
materializer accept the renamed table. The archive keeps every inline row, including quarantined revisions' rows;
drop it yourself once you no longer need it. Reclaiming the space of the original table needs the drop.

Upgrade note: the pre-ownership `migrate_legacy_reporting` and `backfill_legacy_reporting` operator APIs write to
`reporting_revision_rows`, so run them before retiring it.

## Object storage bindings

Rows can live in object storage instead of PostgreSQL. A binding is defined in host code, which makes the
configuration the allowlist: the database records which binding each revision used and its identity digest, but it
can never point reads or writes at a destination the host did not configure.

```python
from adcp.reporting.ledger import (
    PgReportingLedgerStore,
    ReportingObjectRowBindingV1,
    ReportingPostgresRowBindingV1,
    ReportingRowStorageOptionsV1,
)
from adcp.reporting.ledger.row_storage_object import create_filesystem_reporting_row_object_provider_v1

store = PgReportingLedgerStore(
    pool=pool,
    row_storage=ReportingRowStorageOptionsV1(
        deployment_namespace="prod-us",
        bindings={
            "inline": ReportingPostgresRowBindingV1(),
            "reports": ReportingObjectRowBindingV1(
                provider="filesystem", location={"root": "reports"}, prefix="adcp-rows", compression="gzip"
            ),
        },
        providers={
            "filesystem": create_filesystem_reporting_row_object_provider_v1(
                roots={"reports": "/var/lib/adcp/rows"}
            )
        },
        select_row_binding=lambda s: "inline" if s.canonical_byte_count < 256 * 1024 else "reports",
    ),
)
await store.create_schema()
```

- Each chunk is one object, written create-only. An object already at a planned key is adopted only when its bytes
  decode to the exact chunk; otherwise the commit fails with `CONTENT_CONFLICT`.
- Keys always start with `{prefix}/{namespace_key}/` and use a hashed account segment, never a raw account ID.
  Templates may use the placeholders in `REPORTING_ROW_KEY_PLACEHOLDERS` and must include `{content_sha256}` and
  `{chunk_index}`. Keep a retired binding configured while revisions still reference it.
- **Upload before the ledger transaction.** `ReportingProducer` stages every object-bound revision first
  (`store.stage_revision_rows`), so a slow store never holds the account lock or a transaction. If you call
  `commit_revision` yourself for an object-bound revision, call `stage_revision_rows` first; `commit_revision`
  refuses to upload inside a ledger transaction.
- An upload runs only while the configuration's worker lease outlives `write_deadline_seconds` (default 30) plus five
  seconds, so a producer needs a lease above 35 seconds (the default is 60). A fenced write intent records every
  object the upload created, and the ledger commit closes the intent in the same transaction; if a sweep or another
  writer got there first, the commit fails and no header is written. Objects of an upload that never committed are
  left for the intent sweep. The lease is read from the database, but it is set from the producer's clock, so keep
  clocks in sync.
- Rows that are byte-identical to a live row set in the same obligation are stored once: the new revision gets its
  own header and binding and references the existing chunks (`rows_shared_from_row_set_id`), so an unchanged pulse
  uploads nothing.
- Every read verifies the stored bytes against the recorded physical digest before decompressing, then verifies
  segments and the revision binding. A missing object is `ROWS_UNAVAILABLE`; different bytes are
  `ROWS_INTEGRITY_FAILED`.
- **The filesystem provider is for development and single-host deployments only.** It exists so tests and local runs
  need no cloud account; it has no replication or durability story beyond the host's disk, and its native version
  needs a filesystem with sub-second timestamps. Use a cloud provider in production.
- The filesystem provider records each write as `inode:birth_ns:mtime_ns:size`, the format the JavaScript provider
  uses, stamping a random sub-millisecond mtime on every write. It needs a filesystem with sub-second mtime and,
  ideally, birth time (ext4, XFS, APFS, NTFS). The `write-unique-version` conformance check fails on a coarse
  filesystem, which means the filesystem is wrong, not the code.
- Custom providers implement `ReportingRowObjectProviderV1`. Run
  `run_reporting_row_object_provider_conformance_v1(provider, location=..., prefix=...)` against a dedicated test
  prefix before using one.

### Cloud providers

Cloud providers wrap official clients that the host constructs with its own credentials, so the extras only need to
be installed where those clients are created: `pip install 'adcp[reporting-gcs]'`, `'adcp[reporting-s3]'` or
`'adcp[reporting-azure]'`. A binding's `location` holds only non-secret coordinates, and its optional
`credential_ref` names one of the clients you injected; an unknown reference fails `UNSAFE_BINDING`. Provider errors
surface only as stable `ReportingRowStoreError` codes. Row buckets are separate from Managed Delivery buckets. A
native version always identifies exactly one write, and every read and delete is pinned to it. An ETag is never
enough, because an identical upload re-created at the same key has the same ETag and a delayed delete would remove
the replacement.

**Google Cloud Storage** (`adcp.reporting.ledger.row_storage_gcs`):

```python
from google.cloud import storage
from adcp.reporting.ledger.row_storage_gcs import create_gcs_reporting_row_object_provider_v1

gcs = create_gcs_reporting_row_object_provider_v1(
    storage=storage.Client(),  # keyless credentials: workload identity or ADC
    clients={"tenant-eu": eu_client},  # optional, selected by credential_ref
)
# ReportingObjectRowBindingV1(provider="gcs", location={"bucket": "acme-adcp-rows"}, prefix="adcp-rows")
# providers={"gcs": gcs}
```

- Writes are single-request uploads with `if_generation_match=0` and a declared MD5. The recorded native version is
  the object generation. Reads request raw stored bytes, so GCS never transcodes them.
- The probe requires uniform bucket-level access and enforced public access prevention. Versioning, soft delete and
  retention policies are allowed. A lifecycle `Delete` rule that can match live objects under the binding prefix
  (no `matchesPrefix`, or one that overlaps it) is refused; rules limited to noncurrent versions are allowed. Keep
  retention-driven deletion in the ledger, not in bucket lifecycle.
- The client needs `storage.buckets.get` for the probe plus object create, get and delete.

**Amazon S3 and S3-compatible stores** (`adcp.reporting.ledger.row_storage_s3`):

```python
import boto3
from adcp.reporting.ledger.row_storage_s3 import create_s3_reporting_row_object_provider_v1

s3 = create_s3_reporting_row_object_provider_v1(client=boto3.client("s3", region_name="us-east-1"))
# ReportingObjectRowBindingV1(provider="s3", location={"bucket": "acme-adcp-rows", "region": "us-east-1"}, ...)
```

- **Bucket versioning is required.** Writes use `put_object` with `IfNoneMatch="*"` (a 409 is retried once). The
  recorded native version is the write-unique `VersionId`; reads pin it, and a delete confirms the exact version
  with `head_object` first, then deletes that version, which removes it permanently without a delete marker. The
  probe refuses any bucket whose versioning status is not `Enabled`, and a write that returns no `VersionId`.
- Endpoint, region, credentials and path style come only from the client you inject; `location` accepts just `bucket`
  and an optional `region`, which the probe checks against the client.
- **Bound the client's own timeouts.** botocore takes timeouts from the client's `Config`, not per call, and the
  call budget cannot stop a thread already blocked on a socket. The probe therefore refuses a client whose
  `connect_timeout` or `read_timeout` is `None` or non-positive. botocore's defaults (60 seconds each) pass; set
  tighter values with `botocore.config.Config(connect_timeout=..., read_timeout=...)`. GCS and Azure carry the budget
  into each call as `timeout=` instead.
- S3-compatible stores may ignore `If-None-Match`, so the probe writes a probe key under the binding prefix twice and
  requires the second write to fail with 412, then deletes it. It also refuses missing or inaccessible buckets, a
  bucket policy reported as public, and enabled lifecycle `Expiration` rules that can match the prefix.
- The client needs `s3:ListBucket`, `s3:GetLifecycleConfiguration`, `s3:GetBucketVersioning`, object put and get,
  `s3:GetObjectVersion` and `s3:DeleteObjectVersion`. `s3:GetBucketPolicyStatus` is optional.

**Azure Blob Storage** (`adcp.reporting.ledger.row_storage_azure`):

```python
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from adcp.reporting.ledger.row_storage_azure import create_azure_blob_reporting_row_object_provider_v1

azure = create_azure_blob_reporting_row_object_provider_v1(
    client=BlobServiceClient("https://acmerows.blob.core.windows.net", credential=DefaultAzureCredential())
)
# ReportingObjectRowBindingV1(provider="azure_blob", location={"account": "acmerows", "container": "rows"}, ...)
```

- **Blob versioning is required** on the storage account. Writes are block-blob uploads with `If-None-Match: *`;
  the recorded native version is the write-unique `version_id`, and a delete removes the base blob when it is still
  that version, then the version itself. The probe refuses containers that allow anonymous read access or that do
  not enforce create-only writes, and a write that returns no `version_id`.
- The client you inject must belong to the binding's `account`.

## Retention

Two maintenance operations keep row storage bounded. Both are safe to run from any worker on a schedule, in bounded
batches; neither runs on its own, so schedule them next to your producer.

```python
await store.sweep_row_write_intents(limit=100)  # abandoned uploads
await store.prune_expired_row_sets(limit=20)  # periods past their retention window
```

**Intent sweeps** resolve uploads whose ledger commit never happened (a crash, a lost lease, a numbering conflict).
An expired write intent moves to `sweeping` under the account lock, so no writer can commit against it. Only the
objects that upload *created* are then deleted, at their recorded native versions. Adopted objects are never recorded
as created, and any object a committed chunk references is skipped. A sweep that cannot delete leaves the intent for
the next pass and reports a stable, secret-free code per failed intent in `causes` (`(row_set_id, code)`), so a
scheduled sweep that keeps failing is diagnosable. Intents of bindings this host does not configure are not this
host's to sweep; they are counted in `skipped_unconfigured` and left for the host that does.

**Row pruning** retires one period's row bytes at a time, in an ordered, resumable sequence:

1. Under the account lock, live row sets become `pruning`. Reads of those revisions now answer `REVISION_NOT_FOUND`,
   exactly like an unknown revision, never as an integrity failure.
2. The period's recorded object versions are deleted outside any transaction. A failure leaves the period `pruning`
   with every locator intact and the next pass resumes.
3. Under the account lock, chunk manifests and PostgreSQL bodies are deleted and the row sets become `pruned`.

A period qualifies once the later of its period end and its newest revision or adjustment is older than
`max(status_retention_days, ledger_record_retention_days)`, and no materializer work is pending for it. The default
`ledger_record_retention_days` is the generation's `status_retention_days`; a shorter value is ignored, because a row
retention shorter than the advertised window is non-conformant.

Python keeps revision headers and their records until the shared Core layout (persistence Stage 3). A pruned row set
stays as a small marker that makes reads answer as expired; the records are already outside the status projection,
whose horizon excludes periods past retention. Versioned buckets, soft delete and retention locks are allowed as
backstops, but a `pruned` period is not "erased" while they keep the old bytes.

## Interoperability with the JavaScript SDK

Both SDKs read and write the same `adcp_reporting_row_*` tables and the same canonical bytes, so chunks written by
either verify in the other. Three things must match between the two deployments:

- **The same `deployment_namespace`** (and therefore the same installation identity) on both sides, or the binding is
  refused with `UNSAFE_BINDING`.
- **The same binding definitions.** A binding's identity digest is `sha256(JCS([kind, provider, identity_config]))`
  over the names the JavaScript SDK uses (`location`, `prefix`, `keyTemplate`), so an object binding that differs in
  location, prefix or key template is a different binding and is refused rather than silently redirected.
- **The row-storage migration applied by this SDK first**, because the JavaScript `adcp_reporting_row_sets` has a
  foreign key to its own obligations table that Python obligations can never satisfy. See "Layout notes".

Until the shared Core layout (persistence Stage 3) each SDK keeps its own revision headers, so what is shared today
is the row sets: JavaScript-written row sets verify through `ReportingRowStorageV1.find` and `read_all` here, and the
other way around.

The acceptance test runs both SDKs against one PostgreSQL schema and one filesystem bucket root, in both directions:

```bash
ADCP_JS_SDK_DIR=/path/to/adcp-client ADCP_PG_TEST_URL=postgresql://... \
    pytest tests/conformance/reporting/test_reporting_row_storage_interop_pg.py
```

It needs Node and a built `adcp-client` checkout (`npm ci && npm run build`); without them it skips. The rows each SDK
wrote are committed under `tests/fixtures/reporting-row-storage-interop-v1/` (refresh them with
`ADCP_UPDATE_INTEROP_FIXTURES=1`), and the replay tests load them into a fresh database and bucket and verify them
here, so Python CI does not need Node. The JavaScript SDK can replay `python-written.json` the same way.

## Host change feed and consumers

`PgReportingLedgerStore(pool=pool, change_feed=True)` also appends one row to the shared `adcp_reporting_changes`
table (PostgreSQL 13 or later, `xid8`) for every obligation, revision and adjustment, in the transaction that writes
the record and after the account lock. The JavaScript SDK writes the same table. It is separate from
`reporting_ledger_changes`, the buyer-facing feed behind `changes_checkpoint` and `changes_after`, until the shared
Core layout.

```python
page = await store.changes_after(account_id="acct_a", kinds=["revision"], limit=500)
for change in page.records:
    ...
await store.save_feed_consumer_cursor("warehouse-sink", page.cursor)  # holds back pruning
await store.changes_after(account_id="acct_a", cursor=page.cursor)
```

- **Account feeds** order by `seq` and truncate each page before the first change whose transaction is not below
  every transaction still in flight (`xid >= pg_snapshot_xmin(pg_current_snapshot())`): a page is the committed prefix
  in `seq` order and stops there, and its cursor never moves past a held-back change. `seq` is taken under the account
  lock, but ordering does not rely on that lock. Truncation is deliberate: a plain `xid < xmin` *filter* would be
  wrong, because `seq` order and `xid` order can differ, and it would hand out a later change while skipping an earlier
  one it excluded. `xmin` is cluster-wide, so **any older open transaction anywhere in the database holds account pages
  back until it ends, and an empty page can mean "caught up" or "held back"**; poll again rather than concluding
  nothing is new. The row scan is still served by the `(account_id, seq)` index. This narrows the window for a second
  writer but does not close it for every interleaving: one writer SDK per database remains the contract.
- **The deployment-wide feed** (no `account_id`) orders by `(xid, seq)` and returns only changes whose transaction is
  older than every transaction still in flight, so a slow commit is never skipped. A long-running transaction delays
  this feed; it never makes it lose a record.
- A cursor older than the retained history raises `ReportingChangeCursorExpiredError` (`code == "CURSOR_EXPIRED"`).
  Resynchronize from `list_current_revisions` and resume from the cursor returned with that read.
- A registered consumer holds back `prune_changes` until it has passed those changes in its own feed order, up to
  `max_feed_hold_days` (default 7) after its last update, so a dead consumer cannot block retention. Schedule
  `prune_changes(change_retention_days=30)` next to the other maintenance. Cursors are the same bytes in both SDKs.

## Host reads

In-process consumers such as pacing, alerts and a warehouse sink read the ledger directly. Both reads return revision
headers; rows come from the verified paged reader.

```python
current = await store.get_current_revision(account_id="acct_a", reporting_obligation_id="robl_1")
page = await store.list_current_revisions(
    account_id="acct_a", period_start_from=start, period_start_to=end, delivery_config_id="daily", limit=100
)
while page.cursor:
    page = await store.list_current_revisions(..., cursor=page.cursor)
```

The current revision is the official revision if one exists, otherwise the snapshot nothing supersedes yet.
`list_current_revisions` pages obligations whose period starts in `[period_start_from, period_start_to)` in period order
and omits obligations without a revision. Its cursor is the format the JavaScript SDK uses. A change-feed consumer
that receives `CURSOR_EXPIRED` resynchronizes from this read.

`list_current_revisions` pages by `(period_start, reporting_obligation_id)`. Enabling `change_feed` creates
`reporting_obligations_account_period_idx` to back that order; a host without it can create the same index itself on
`reporting_obligations (account_id, period_start, reporting_obligation_id)`. A cursor whose position is not a
timestamp with a zone raises `ValueError`, never a driver error.

## Period retirement

`store.retire_expired_periods()` retires a whole period once its retention window has elapsed, as one ordered,
resumable sequence that never deletes the records that locate row bytes while the bytes remain stored:

1. **Tombstone.** Under the account lock, a `retiring` tombstone records the final revision, its digests, and the
   revision and adjustment counts, and the period's row sets stop being served.
2. **External bytes.** The period's recorded object versions are deleted. A failure leaves the period `retiring` with
   every locator intact; the next pass resumes `retiring` periods before new candidates.
3. **Records.** Under the account lock the manifests, bodies and the period's ledger records are deleted, the
   tombstone becomes `retired`, and a `retirement` change is appended when the change feed is on.

A period qualifies once the later of its period end, its newest revision's `created_at` and its newest adjustment's
`created_at`, plus `max(status_retention_days, ledger_record_retention_days)`, has passed. It is held, and simply not a
candidate this pass, while a materialization, a receipt or pending materializer work references it, or while a
registered feed consumer updated within `max_feed_hold_days` has not passed its change rows. A silent consumer stops
holding after `max_feed_hold_days`, so retention is never blocked forever. Eligibility is re-checked under the account
lock before the tombstone is written, so a late revision cannot be retired with a stale candidate. A tombstoned period
is never recreated or extended: `commit_obligation`, `commit_revision` and `commit_adjustment` refuse it with
`PERIOD_RETIRED`.

**Current limit: record deletion is effectively off for production deployments.** Several Python extension tables
(provisional observations, reconciliation records, notification events) are append-only by trigger and foreign key, and
their guards are frozen catalog objects. Every production period carries reconciliation records, and a reconciliation
record *holds* its period, so retirement never even picks those periods up; a period whose protected records are
provisional observations or notification events finishes phases 1 and 2 and then stays `retiring`, reported in
`protected`. Either way the **row bytes are freed** (the worker schedules `prune_expired_row_sets` for exactly this),
reads answer as expired, and the records stay. Phase 3 deletes every record of a period or none. Relaxing those guards
is an audit-model decision for the maintainers, deliberately not made here; until it is, plan storage for the records
themselves, which are small headers and receipts, not row bytes.

Stuck periods never crowd out the rest, with the same names and numbers as the JavaScript SDK. A period that cannot
finish records `attempts`, `next_attempt_at` and `last_failure` (a stable code, `RECORDS_PROTECTED` for protected
records) on its tombstone and backs off: 1 minute, doubling, capped at 6 hours. Ready retries take at most half of a
pass, rounded up, and fresh periods fill the rest; retries only backfill slots fresh periods leave empty. The result
reports what was left alone: `protected` (this pass), `held` (expired periods a hold kept out, bounded count),
`deferred` (retiring periods backing off, not attempted this pass) and `stuck` (periods with five or more failed
attempts, worst first, at most 20: `(reporting_obligation_id, attempts, cause)`).

Tombstones live in `adcp_reporting_period_tombstones`, created with row storage or the change feed.

## Scheduling

`ReportingProductionSupport` runs the maintenance for the features your store opted into, every
`maintenance_seconds` (default 300; `None` disables it), on the same worker loop as the producer and materializer:

| Store option | Operation | Bound |
| --- | --- | --- |
| `row_storage` | `sweep_row_write_intents` | 100 intents |
| `row_storage` | `prune_expired_row_sets` | 20 periods |
| `row_storage` or `change_feed` | `retire_expired_periods` | 20 periods |
| `change_feed` | `prune_changes` | 10,000 rows |

Each operation is bounded and resumable. A failing operation never stops the worker: the next interval retries it.
Only the closed operation name and integer counters are logged, never an exception, provider message or identifier:

- `REPORTING_MAINTENANCE_FAILED` (warning): the operation raised or reported failed items (a binding that cannot
  delete, for example). Read `causes` from the result for the stable per-item code.
- `REPORTING_MAINTENANCE_PENDING` (info): work was left in place on purpose, with the counters `protected`, `held`,
  `deferred` and `skipped_unconfigured`. A retirement that does nothing because every period is held shows up here
  instead of looking like a healthy idle worker.
- `REPORTING_MAINTENANCE_STALLED` (warning): work nobody is reaching, with `overdue` (intents expired for more than
  `overdue_after_seconds`, default 24 hours, and still unswept: a binding no host configures any more, or one that
  keeps failing, so objects leak) and `stuck` (periods with five or more failed retirement attempts).

The last pass's counters are also on `ReportingProductionSupport.maintenance_report`, per operation.
`prune_expired_row_sets` is scheduled so row bytes are reclaimed even while a period's records stay; its only hold is
pending materializer work. The intent sweep skips bindings this host does not configure (counted in
`skipped_unconfigured`, unless an intent has no recorded objects, which needs no binding) and backs a failing intent
off (`sweep_attempts` and `next_sweep_at` on the shared write-intent table, the same columns the JavaScript SDK adds:
1 minute, doubling, capped at 6 hours; counted as `deferred`), trying never-failed intents first, so one broken binding
cannot starve the others.
Outside the production service, call the same methods from your own scheduler.
