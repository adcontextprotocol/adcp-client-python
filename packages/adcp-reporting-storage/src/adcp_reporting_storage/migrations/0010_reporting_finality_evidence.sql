-- adcp-migration: 0010 reporting_finality_evidence
-- adcp-upgrade: additive
-- Reused snapshot observations have separate append-only storage so published
-- workers retain the revision identity constraint during a rolling deployment.
-- include: ledger/reporting_provisional_reuse.sql

CREATE TABLE IF NOT EXISTS adcp_reporting_stabilization_checkpoints (
    reporting_obligation_id TEXT COLLATE "C" PRIMARY KEY
        REFERENCES reporting_restatement_checkpoints (reporting_obligation_id) ON DELETE CASCADE,
    next_observation BIGINT NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL,
    stabilization_policy JSONB,
    next_stabilization_due_at TIMESTAMPTZ
);
