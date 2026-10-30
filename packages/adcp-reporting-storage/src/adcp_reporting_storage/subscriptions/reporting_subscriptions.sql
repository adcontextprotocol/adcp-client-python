-- AdCP 3.3 reporting subscriptions: durable principal records and engine state.
-- Each row is one JSON document per subject, replaced by compare-and-swap on
-- its revision. Rows are never pruned here: the documents keep every
-- subscription version for as long as the obligations that cite it.

CREATE TABLE IF NOT EXISTS adcp_principal_records (
    subject TEXT PRIMARY KEY,
    store_revision BIGINT NOT NULL CHECK (store_revision >= 1),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS adcp_reporting_subscription_state (
    subject TEXT PRIMARY KEY,
    revision BIGINT NOT NULL CHECK (revision >= 1),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
