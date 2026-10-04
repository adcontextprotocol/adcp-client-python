-- Opt-in sibling of the task and reporting outboxes; no existing-table migration.
-- Apply through PgNotificationOutbox.create_schema() or your migration tool.
CREATE TABLE IF NOT EXISTS adcp_notification_outbox (
    id BIGSERIAL PRIMARY KEY,
    notification_type TEXT COLLATE "C" NOT NULL,
    caller_scope_id TEXT COLLATE "C" NOT NULL,
    account_id TEXT COLLATE "C",
    url TEXT NOT NULL,
    idempotency_key TEXT COLLATE "C" NOT NULL,
    -- SHA-256 of [caller, account (or null), destination, notification type].
    dedup_scope BYTEA NOT NULL,
    signing_scope_id TEXT COLLATE "C",
    encryption_key_id TEXT COLLATE "C" NOT NULL,
    encrypted_body BYTEA NOT NULL,
    envelope_nonce BYTEA NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_token TEXT COLLATE "C",
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retry_until TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    last_http_status INTEGER,
    last_error TEXT,
    UNIQUE (dedup_scope, idempotency_key),
    CHECK (state IN ('pending', 'in_flight', 'delivered', 'expired', 'invalid')),
    CHECK (attempt_count >= 0),
    CHECK (octet_length(envelope_nonce) = 12),
    CHECK (octet_length(dedup_scope) = 32),
    CHECK (account_id IS NULL OR account_id <> '')
);
CREATE INDEX IF NOT EXISTS adcp_notification_outbox_work_idx
    ON adcp_notification_outbox (available_at, id)
    WHERE state IN ('pending', 'in_flight');
CREATE INDEX IF NOT EXISTS adcp_notification_outbox_retry_until_idx
    ON adcp_notification_outbox (retry_until);
