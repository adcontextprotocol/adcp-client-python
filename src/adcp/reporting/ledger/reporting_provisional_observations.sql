-- Additive private scheduling metadata. Original revision rows and manifests
-- remain readable by historical SDKs; no existing payload is rewritten.
CREATE TABLE IF NOT EXISTS reporting_provisional_acquisitions (
    account_id TEXT COLLATE "C" NOT NULL,
    reporting_obligation_id TEXT COLLATE "C" NOT NULL
        REFERENCES reporting_obligations (reporting_obligation_id),
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    source_execution_key TEXT COLLATE "C" NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (account_id, reporting_obligation_id, ordinal),
    UNIQUE (account_id, source_execution_key)
);

CREATE TABLE IF NOT EXISTS reporting_provisional_observations (
    account_id TEXT COLLATE "C" NOT NULL,
    reporting_obligation_id TEXT COLLATE "C" NOT NULL,
    ordinal BIGINT NOT NULL,
    reporting_revision_id TEXT COLLATE "C" NOT NULL UNIQUE
        REFERENCES reporting_revisions (reporting_revision_id),
    payload JSONB NOT NULL,
    PRIMARY KEY (account_id, reporting_obligation_id, ordinal),
    FOREIGN KEY (account_id, reporting_obligation_id, ordinal)
        REFERENCES reporting_provisional_acquisitions
            (account_id, reporting_obligation_id, ordinal)
);

CREATE OR REPLACE FUNCTION reporting_provisional_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'provisional observation metadata is immutable';
END;
$$;

DROP TRIGGER IF EXISTS reporting_provisional_acquisition_immutable
    ON reporting_provisional_acquisitions;
CREATE TRIGGER reporting_provisional_acquisition_immutable
    BEFORE UPDATE OR DELETE ON reporting_provisional_acquisitions
    FOR EACH ROW EXECUTE FUNCTION reporting_provisional_immutable();
DROP TRIGGER IF EXISTS reporting_provisional_observation_immutable
    ON reporting_provisional_observations;
CREATE TRIGGER reporting_provisional_observation_immutable
    BEFORE UPDATE OR DELETE ON reporting_provisional_observations
    FOR EACH ROW EXECUTE FUNCTION reporting_provisional_immutable();
