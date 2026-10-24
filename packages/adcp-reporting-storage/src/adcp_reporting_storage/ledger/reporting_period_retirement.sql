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
    next_attempt_at         TIMESTAMPTZ,
    last_failure            TEXT,
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);
-- A period that cannot finish (protected records, a failing provider) backs off exponentially
-- (1 minute, doubling, capped at 6 hours) instead of occupying a candidate slot on every pass.
-- Same column names and semantics as the JavaScript tombstones; NULL next_attempt_at means due.
-- Earlier installs are upgraded in place, including the first Python spelling of these columns.
ALTER TABLE adcp_reporting_period_tombstones
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0);
ALTER TABLE adcp_reporting_period_tombstones ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE adcp_reporting_period_tombstones ALTER COLUMN next_attempt_at DROP NOT NULL;
ALTER TABLE adcp_reporting_period_tombstones ALTER COLUMN next_attempt_at DROP DEFAULT;
ALTER TABLE adcp_reporting_period_tombstones ADD COLUMN IF NOT EXISTS last_failure TEXT;
DO $failure$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema() AND table_name = 'adcp_reporting_period_tombstones'
       AND column_name = 'last_outcome'
  ) THEN
    UPDATE adcp_reporting_period_tombstones SET last_failure = last_outcome
     WHERE last_failure IS NULL AND last_outcome IS NOT NULL;
    ALTER TABLE adcp_reporting_period_tombstones DROP COLUMN last_outcome;
  END IF;
END
$failure$;
DROP INDEX IF EXISTS adcp_reporting_period_tombstones_due;
CREATE INDEX IF NOT EXISTS adcp_reporting_period_tombstones_due
    ON adcp_reporting_period_tombstones (attempts, period_end)
    WHERE state = 'retiring';
CREATE INDEX IF NOT EXISTS adcp_reporting_period_tombstones_account
    ON adcp_reporting_period_tombstones (account_id, delivery_config_id, period_start);

-- The first Python spelling of per-intent sweep backoff, replaced by sweep_attempts and
-- next_sweep_at on the shared write-intent table (the same columns the JavaScript SDK uses).
DROP TABLE IF EXISTS adcp_reporting_row_sweep_backoff;
