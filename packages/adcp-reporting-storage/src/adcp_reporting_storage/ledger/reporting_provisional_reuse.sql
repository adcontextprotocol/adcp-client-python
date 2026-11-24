CREATE TABLE IF NOT EXISTS adcp_reporting_reused_observations (
    account_id TEXT COLLATE "C" NOT NULL,
    reporting_obligation_id TEXT COLLATE "C" NOT NULL,
    ordinal BIGINT NOT NULL,
    reporting_revision_id TEXT COLLATE "C" NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (account_id, reporting_obligation_id, ordinal),
    CONSTRAINT reused_observation_revision_fk
        FOREIGN KEY (account_id, reporting_revision_id)
        REFERENCES reporting_revisions (account_id, reporting_revision_id),
    CONSTRAINT reused_observation_acquisition_fk
        FOREIGN KEY (account_id, reporting_obligation_id, ordinal)
        REFERENCES reporting_provisional_acquisitions
            (account_id, reporting_obligation_id, ordinal)
);

DROP TRIGGER IF EXISTS adcp_reporting_reused_observation_immutable
    ON adcp_reporting_reused_observations;
CREATE TRIGGER adcp_reporting_reused_observation_immutable
    BEFORE UPDATE OR DELETE ON adcp_reporting_reused_observations
    FOR EACH ROW EXECUTE FUNCTION reporting_provisional_immutable();

CREATE OR REPLACE VIEW adcp_reporting_provisional_history AS
SELECT account_id, reporting_obligation_id, ordinal, reporting_revision_id, payload
FROM reporting_provisional_observations
UNION ALL
SELECT account_id, reporting_obligation_id, ordinal, reporting_revision_id, payload
FROM adcp_reporting_reused_observations;
