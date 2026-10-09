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
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS adcp_reporting_period_tombstones_account
    ON adcp_reporting_period_tombstones (account_id, delivery_config_id, period_start);
