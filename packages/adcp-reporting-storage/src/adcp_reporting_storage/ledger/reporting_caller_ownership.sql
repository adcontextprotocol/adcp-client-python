-- Operator-only maintenance records. No synthetic owner is ever assigned.
CREATE TABLE IF NOT EXISTS adcp_reporting_ownership_archives (
    archive_schema TEXT COLLATE "C" PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS adcp_reporting_ownership_backfills (
    archive_schema TEXT COLLATE "C" NOT NULL REFERENCES adcp_reporting_ownership_archives,
    account_id TEXT COLLATE "C" NOT NULL,
    delivery_config_id TEXT COLLATE "C" NOT NULL,
    delivery_config_version INTEGER NOT NULL,
    consumer_id TEXT COLLATE "C" NOT NULL,
    evidence_sha256 TEXT COLLATE "C" NOT NULL,
    authority_reference TEXT NOT NULL,
    pending_work_disposition TEXT NOT NULL CHECK (pending_work_disposition='retain_without_replay'),
    PRIMARY KEY (archive_schema, account_id, delivery_config_id, delivery_config_version)
);
