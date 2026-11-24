-- adcp-migration: 0010 reporting_finality_evidence
-- Scheduled observations may retain the same snapshot revision, and
-- stabilization evidence is stored beside the existing restatement cursor.
-- The constraint change widens valid observations; earlier writers continue
-- to write distinct revisions during a rolling deployment.
ALTER TABLE reporting_provisional_observations
    DROP CONSTRAINT IF EXISTS reporting_provisional_observations_reporting_revision_id_key;

CREATE TABLE IF NOT EXISTS adcp_reporting_stabilization_checkpoints (
    reporting_obligation_id TEXT COLLATE "C" PRIMARY KEY
        REFERENCES reporting_restatement_checkpoints (reporting_obligation_id) ON DELETE CASCADE,
    next_observation BIGINT NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL,
    stabilization_policy JSONB,
    next_stabilization_due_at TIMESTAMPTZ
);
