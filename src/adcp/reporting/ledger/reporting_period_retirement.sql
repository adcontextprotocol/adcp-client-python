-- Period tombstones for reporting period retirement.
--
-- A retired period keeps one compact record: it refuses replays that would resurrect
-- the period, and it preserves the audit conclusion after the period's ledger records
-- are deleted (adcontextprotocol/adcp#7996, section 4.2). Applied when row storage or the
-- change feed is enabled. Python only: the JavaScript tombstone table is keyed by its own
-- configuration identity, so this one keeps the Python generation key until the shared
-- Core layout (persistence Stage 3).

SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));

CREATE TABLE IF NOT EXISTS adcp_reporting_period_tombstones (
    reporting_obligation_id TEXT COLLATE "C" NOT NULL PRIMARY KEY,
    account_id              TEXT COLLATE "C" NOT NULL,
    consumer_id             TEXT COLLATE "C" NOT NULL,
    delivery_config_id      TEXT COLLATE "C" NOT NULL,
    delivery_config_version INTEGER NOT NULL,
    period_start            TIMESTAMPTZ NOT NULL,
    period_end              TIMESTAMPTZ NOT NULL,
    state                   TEXT NOT NULL CHECK (state IN ('retiring', 'retired')),
    final_revision_id       TEXT COLLATE "C",
    final_content_sha256    TEXT COLLATE "C",
    final_row_manifest_sha256 TEXT COLLATE "C",
    revision_count          INTEGER NOT NULL CHECK (revision_count >= 0),
    adjustment_count        INTEGER NOT NULL CHECK (adjustment_count >= 0),
    retiring_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    retired_at              TIMESTAMPTZ,
    attempts                INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_outcome            TEXT,
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);
-- A period that cannot finish (protected records, a failing provider) backs off exponentially
-- instead of occupying a candidate slot on every pass. Added in place for earlier installs.
ALTER TABLE adcp_reporting_period_tombstones
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0);
ALTER TABLE adcp_reporting_period_tombstones
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();
ALTER TABLE adcp_reporting_period_tombstones
    ADD COLUMN IF NOT EXISTS last_outcome TEXT;
CREATE INDEX IF NOT EXISTS adcp_reporting_period_tombstones_due
    ON adcp_reporting_period_tombstones (next_attempt_at, period_end)
    WHERE state = 'retiring';
CREATE INDEX IF NOT EXISTS adcp_reporting_period_tombstones_account
    ON adcp_reporting_period_tombstones (account_id, delivery_config_id, period_start);

-- Per-intent failure backoff for the intent sweep, so one binding that keeps failing cannot
-- occupy every slot of every pass and starve the intents of other bindings. Python only: the
-- shared write-intent table is not altered.
CREATE TABLE IF NOT EXISTS adcp_reporting_row_sweep_backoff (
    row_binding_id  TEXT COLLATE "C" NOT NULL,
    row_set_id      TEXT COLLATE "C" NOT NULL,
    content_sha256  TEXT COLLATE "C" NOT NULL,
    attempts        INTEGER NOT NULL CHECK (attempts > 0),
    next_attempt_at TIMESTAMPTZ NOT NULL,
    last_cause      TEXT NOT NULL,
    PRIMARY KEY (row_binding_id, row_set_id, content_sha256)
);
