-- Additive private scheduling metadata. Original revision rows and manifests
-- remain readable by historical SDKs; no existing payload is rewritten.
-- Apply after reporting_ledger_reconciliation.sql installs the prerequisite
-- unique indexes reporting_obligations_account_identity and
-- reporting_revisions_account_identity for the account-bound foreign keys.
CREATE TABLE IF NOT EXISTS reporting_provisional_acquisitions (
    account_id TEXT COLLATE "C" NOT NULL,
    reporting_obligation_id TEXT COLLATE "C" NOT NULL,
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    source_execution_key TEXT COLLATE "C" NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (account_id, reporting_obligation_id, ordinal),
    CONSTRAINT provisional_acquisition_execution_key UNIQUE (account_id, source_execution_key),
    CONSTRAINT provisional_acquisition_owner_fk
        FOREIGN KEY (account_id, reporting_obligation_id)
        REFERENCES reporting_obligations (account_id, reporting_obligation_id)
);

CREATE TABLE IF NOT EXISTS reporting_provisional_observations (
    account_id TEXT COLLATE "C" NOT NULL,
    reporting_obligation_id TEXT COLLATE "C" NOT NULL,
    ordinal BIGINT NOT NULL,
    reporting_revision_id TEXT COLLATE "C" NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    PRIMARY KEY (account_id, reporting_obligation_id, ordinal),
    CONSTRAINT provisional_observation_revision_fk
        FOREIGN KEY (account_id, reporting_revision_id)
        REFERENCES reporting_revisions (account_id, reporting_revision_id),
    CONSTRAINT provisional_observation_acquisition_fk
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
