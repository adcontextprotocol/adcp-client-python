-- Buyer-only additive persistence. Use a dedicated buyer schema/pool.
-- Never expire/delete a pending intent: uncertainty has no safe time limit.
CREATE TABLE IF NOT EXISTS reporting_buyer_submission_scopes (
    scope_sha256 text NOT NULL,
    canonical_identity text NOT NULL,
    current_submission_id text,
    CONSTRAINT reporting_buyer_submission_scopes_pkey PRIMARY KEY (scope_sha256),
    CONSTRAINT reporting_buyer_scope_digest CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    CONSTRAINT reporting_buyer_scope_bound CHECK (octet_length(canonical_identity) <= 32768)
);

CREATE TABLE IF NOT EXISTS reporting_buyer_submission_intents (
    scope_sha256 text NOT NULL,
    submission_id text NOT NULL,
    canonical_plan text NOT NULL,
    plan_sha256 text NOT NULL,
    confirmed_results text NOT NULL,
    confirmed_sha256 text NOT NULL,
    pending boolean NOT NULL,
    CONSTRAINT reporting_buyer_submission_intents_pkey PRIMARY KEY (scope_sha256, submission_id),
    CONSTRAINT reporting_buyer_submission_scope_fk FOREIGN KEY (scope_sha256)
        REFERENCES reporting_buyer_submission_scopes (scope_sha256),
    CONSTRAINT reporting_buyer_submission_id CHECK (
        submission_id ~ '^reporting-submission:[a-f0-9]{64}$'
    ),
    CONSTRAINT reporting_buyer_plan_digest CHECK (plan_sha256 ~ '^[a-f0-9]{64}$'),
    CONSTRAINT reporting_buyer_confirmed_digest CHECK (confirmed_sha256 ~ '^[a-f0-9]{64}$'),
    CONSTRAINT reporting_buyer_plan_bound CHECK (octet_length(canonical_plan) <= 16777216),
    CONSTRAINT reporting_buyer_confirmed_bound CHECK (octet_length(confirmed_results) <= 16777216)
);

CREATE UNIQUE INDEX IF NOT EXISTS reporting_buyer_one_pending_scope
    ON reporting_buyer_submission_intents (scope_sha256) WHERE pending;
