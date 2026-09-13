-- AdCP Reliable Reporting ledger — durable obligations, revisions, and status.
--
-- Run this once per deployment, or call
-- PgReportingLedgerStore.create_schema() from application code, which runs the
-- equivalent DDL idempotently on boot.
--
-- COLLATE "C" on identifier columns avoids locale-dependent case folding — on
-- some locales "Buy-A" and "buy-a" compare equal, which would collapse two
-- distinct accounts or revisions into one. "C" is the byte-for-byte comparison
-- reporting evidence actually requires.
--
-- Every immutable write also appends to reporting_ledger_changes in the same
-- transaction. That feed is what makes `changes_after` exact: a consumer that
-- persists a checkpoint and replays from it cannot miss a record or see the
-- same record twice under a different identity.

CREATE TABLE IF NOT EXISTS reporting_configurations (
    delivery_config_id      TEXT COLLATE "C" NOT NULL,
    delivery_config_version INTEGER          NOT NULL,
    account_id              TEXT COLLATE "C" NOT NULL,
    report_definition_id    TEXT COLLATE "C" NOT NULL,
    reporting_profile       TEXT             NOT NULL,
    feed_purpose            TEXT             NOT NULL,
    required_finality       TEXT             NOT NULL,
    account_timezone        TEXT             NOT NULL DEFAULT 'UTC',
    schedule                JSONB            NOT NULL,
    media_buy_ids           JSONB            NOT NULL DEFAULT '[]'::jsonb,
    activated_at            TIMESTAMPTZ,
    deactivated_at          TIMESTAMPTZ,
    automated_recovery_seconds DOUBLE PRECISION NOT NULL DEFAULT 21600,
    status_retention_days   INTEGER          NOT NULL DEFAULT 400,
    -- Binds the whole generation so a re-put with changed content is a
    -- detectable conflict rather than a silent edit of retained evidence.
    content_sha256          TEXT COLLATE "C" NOT NULL,
    -- Period-close leasing. A worker that dies mid-close releases its work by
    -- expiry instead of wedging the period forever.
    lease_worker_id         TEXT COLLATE "C",
    lease_expires_at        TIMESTAMPTZ,
    PRIMARY KEY (delivery_config_id, delivery_config_version)
);

CREATE INDEX IF NOT EXISTS reporting_configurations_account_idx
    ON reporting_configurations (account_id);

-- Supports "lease the least recently worked generation" without a full scan.
CREATE INDEX IF NOT EXISTS reporting_configurations_lease_idx
    ON reporting_configurations (lease_expires_at NULLS FIRST);

CREATE TABLE IF NOT EXISTS reporting_obligations (
    reporting_obligation_id TEXT COLLATE "C" NOT NULL PRIMARY KEY,
    account_id              TEXT COLLATE "C" NOT NULL,
    delivery_config_id      TEXT COLLATE "C" NOT NULL,
    delivery_config_version INTEGER          NOT NULL,
    report_definition_id    TEXT COLLATE "C" NOT NULL,
    reporting_profile       TEXT             NOT NULL,
    feed_purpose            TEXT             NOT NULL,
    period_key              TEXT             NOT NULL,
    period_start            TIMESTAMPTZ      NOT NULL,
    period_end              TIMESTAMPTZ      NOT NULL,
    source_timezone         TEXT             NOT NULL,
    expected_at             TIMESTAMPTZ      NOT NULL,
    scope_resolved_at       TIMESTAMPTZ      NOT NULL,
    automated_recovery_deadline_at TIMESTAMPTZ NOT NULL,
    required_finality       TEXT             NOT NULL,
    coverage_status         TEXT             NOT NULL DEFAULT 'full',
    media_buy_ids           JSONB            NOT NULL DEFAULT '[]'::jsonb,
    package_ids             JSONB            NOT NULL DEFAULT '[]'::jsonb,
    schedule                JSONB            NOT NULL,
    created_at              TIMESTAMPTZ      NOT NULL
);

-- One obligation per logical period. Without this, two workers racing a period
-- close could commit two obligations and a seller could quietly publish twice
-- and pick a winner.
CREATE UNIQUE INDEX IF NOT EXISTS reporting_obligations_period_key
    ON reporting_obligations
       (account_id, delivery_config_id, delivery_config_version, period_start, period_end);

CREATE INDEX IF NOT EXISTS reporting_obligations_account_idx
    ON reporting_obligations (account_id, period_end DESC);

CREATE TABLE IF NOT EXISTS reporting_revisions (
    reporting_revision_id   TEXT COLLATE "C" NOT NULL PRIMARY KEY,
    account_id              TEXT COLLATE "C" NOT NULL,
    reporting_obligation_id TEXT COLLATE "C" NOT NULL
        REFERENCES reporting_obligations (reporting_obligation_id),
    finality                TEXT             NOT NULL,
    revision_content_sha256 TEXT COLLATE "C" NOT NULL,
    row_count               BIGINT           NOT NULL,
    control_totals          JSONB            NOT NULL DEFAULT '[]'::jsonb,
    observed_at             TIMESTAMPTZ      NOT NULL,
    data_through            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ      NOT NULL,
    supersedes_reporting_revision_id TEXT COLLATE "C",
    finality_basis          TEXT,
    finality_policy_id      TEXT,
    finalized_at            TIMESTAMPTZ,
    -- Core promises a committed revision stays readable for
    -- status_retention_days. `readable` records reality; `readable_at_commit`
    -- remembers that it once was, so the health projection can name the
    -- revision that opened the gap rather than an arbitrary one.
    readable                BOOLEAN          NOT NULL DEFAULT TRUE,
    readable_at_commit      BOOLEAN          NOT NULL DEFAULT TRUE,
    source_publication_id   TEXT COLLATE "C",
    source_manifest_sha256  TEXT COLLATE "C",
    -- Binds what was published, excluding mutable readability.
    content_sha256          TEXT COLLATE "C" NOT NULL
);

-- An official revision is terminal: at most one per obligation. A later source
-- correction is an adjustment, never a second official close.
CREATE UNIQUE INDEX IF NOT EXISTS reporting_revisions_one_official
    ON reporting_revisions (reporting_obligation_id)
    WHERE finality = 'official';

-- Supersession must not fork: a given revision may be superseded at most once.
CREATE UNIQUE INDEX IF NOT EXISTS reporting_revisions_one_successor
    ON reporting_revisions (supersedes_reporting_revision_id)
    WHERE supersedes_reporting_revision_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS reporting_revisions_obligation_idx
    ON reporting_revisions (account_id, reporting_obligation_id);

CREATE TABLE IF NOT EXISTS reporting_revision_rows (
    reporting_revision_id TEXT COLLATE "C" NOT NULL
        REFERENCES reporting_revisions (reporting_revision_id),
    ordinal               BIGINT           NOT NULL,
    row_payload           JSONB            NOT NULL,
    PRIMARY KEY (reporting_revision_id, ordinal)
);

CREATE TABLE IF NOT EXISTS reporting_adjustments (
    reporting_adjustment_id TEXT COLLATE "C" NOT NULL PRIMARY KEY,
    account_id              TEXT COLLATE "C" NOT NULL,
    adjusts_reporting_revision_id TEXT COLLATE "C" NOT NULL
        REFERENCES reporting_revisions (reporting_revision_id),
    reason_code             TEXT             NOT NULL,
    reason_detail           TEXT,
    accounting_period_start TIMESTAMPTZ      NOT NULL,
    accounting_period_end   TIMESTAMPTZ      NOT NULL,
    control_total_deltas    JSONB            NOT NULL DEFAULT '[]'::jsonb,
    correction_observed_at  TIMESTAMPTZ      NOT NULL,
    created_at              TIMESTAMPTZ      NOT NULL
);

CREATE INDEX IF NOT EXISTS reporting_adjustments_revision_idx
    ON reporting_adjustments (account_id, adjusts_reporting_revision_id);

-- PREVIEW: sync_reporting_status. Created unconditionally because DDL is
-- cheap and a migration mid-rollout is not; the ingest that writes here is off
-- until an adopter enables it. See adcp/reporting/ledger/consumer_status.py.
CREATE TABLE IF NOT EXISTS reporting_consumer_statuses (
    reporting_status_id     TEXT COLLATE "C" NOT NULL PRIMARY KEY,
    account_id              TEXT COLLATE "C" NOT NULL,
    -- Derived from authenticated transport, never from the request body.
    consumer_id             TEXT COLLATE "C" NOT NULL,
    delivery_config_id      TEXT COLLATE "C" NOT NULL,
    delivery_config_version INTEGER          NOT NULL,
    report_definition_id    TEXT COLLATE "C" NOT NULL,
    period_start            TIMESTAMPTZ      NOT NULL,
    period_end              TIMESTAMPTZ      NOT NULL,
    period_source_timezone  TEXT             NOT NULL,
    consumer_status         TEXT             NOT NULL,
    status_as_of            TIMESTAMPTZ      NOT NULL,
    recorded_at             TIMESTAMPTZ      NOT NULL,
    supersedes_reporting_status_id TEXT COLLATE "C",
    -- Nullable by design: obligation_missing is filed precisely when the
    -- seller's ledger omitted the period, so requiring a seller-issued
    -- obligation id would make the first missing report invisible again.
    reporting_obligation_id TEXT COLLATE "C",
    reporting_revision_id   TEXT COLLATE "C",
    observed_revision_content_sha256 TEXT COLLATE "C",
    failure_code            TEXT,
    consumer_commit_ref     TEXT,
    seller_ledger_snapshot_id TEXT,
    seller_ledger_as_of     TIMESTAMPTZ,
    superseded              BOOLEAN          NOT NULL DEFAULT FALSE,
    content_sha256          TEXT COLLATE "C" NOT NULL
);

-- Exactly one unsuperseded leaf per logical chain. This is what makes
-- supersession atomic: a concurrent update naming a stale leaf hits this
-- constraint instead of forking the chain, so a successful retry cannot erase
-- a recorded outage.
CREATE UNIQUE INDEX IF NOT EXISTS reporting_consumer_statuses_one_leaf
    ON reporting_consumer_statuses
       (account_id, consumer_id, delivery_config_id, delivery_config_version,
        report_definition_id, period_start, period_end)
    WHERE superseded = FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS reporting_consumer_statuses_one_successor
    ON reporting_consumer_statuses (supersedes_reporting_status_id)
    WHERE supersedes_reporting_status_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS reporting_consumer_statuses_chain_idx
    ON reporting_consumer_statuses (account_id, consumer_id, period_end DESC);

-- The per-account change feed. `seq` orders every immutable record across
-- kinds so `changes_after` is exact.
--
-- Appends take a transaction-scoped advisory lock on the account, which makes
-- sequence order equal commit order *within an account*. Without it, a
-- transaction that grabbed a low sequence but committed late would be invisible
-- to a consumer that had already checkpointed past it — a silently lost record,
-- which is the one failure a reporting ledger must not have. Contention is
-- per-account and the appends are short.
CREATE TABLE IF NOT EXISTS reporting_ledger_changes (
    seq          BIGSERIAL        NOT NULL PRIMARY KEY,
    account_id   TEXT COLLATE "C" NOT NULL,
    record_kind  TEXT             NOT NULL,
    record_id    TEXT COLLATE "C" NOT NULL,
    committed_at TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS reporting_ledger_changes_record
    ON reporting_ledger_changes (account_id, record_kind, record_id);

CREATE INDEX IF NOT EXISTS reporting_ledger_changes_feed_idx
    ON reporting_ledger_changes (account_id, seq);
