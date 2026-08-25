-- AdCP terminal task-webhook outbox.
--
-- Terminal task state and an outbox row must be committed in one transaction.
-- PgTaskRegistry performs that write when configured with
-- PgTaskWebhookOutbox. The body is encrypted and authenticated with
-- application-held AES-256-GCM key material; retry_until begins at the first
-- attempt and preserves the immutable binding after successful delivery.

CREATE TABLE IF NOT EXISTS adcp_task_webhook_outbox (
    id                 BIGSERIAL PRIMARY KEY,
    task_id            TEXT COLLATE "C" NOT NULL UNIQUE,
    account_id         TEXT COLLATE "C" NOT NULL,
    task_type          TEXT NOT NULL,
    terminal_status    TEXT NOT NULL,
    url                TEXT NOT NULL,
    operation_id       TEXT NOT NULL,
    idempotency_key    TEXT COLLATE "C" NOT NULL UNIQUE,
    -- Trusted server-side tenant/key scope. NULL preserves fixed-sender rows.
    signing_scope_id   TEXT COLLATE "C",
    encrypted_body     BYTEA NOT NULL,
    envelope_nonce     BYTEA NOT NULL,
    state              TEXT NOT NULL DEFAULT 'pending',
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    available_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_token        TEXT COLLATE "C",
    lease_expires_at   TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_attempt_at   TIMESTAMPTZ,
    retry_until        TIMESTAMPTZ,
    retry_horizon_seconds INTEGER NOT NULL,
    delivered_at       TIMESTAMPTZ,
    last_http_status   INTEGER,
    last_error         TEXT,
    CHECK (state IN ('pending', 'in_flight', 'delivered', 'expired', 'invalid')),
    CHECK (terminal_status IN ('completed', 'failed')),
    CHECK (attempt_count >= 0),
    CHECK (octet_length(envelope_nonce) = 12),
    CHECK (retry_horizon_seconds BETWEEN 86400 AND 604800),
    CHECK ((first_attempt_at IS NULL) = (retry_until IS NULL)),
    CHECK (retry_until IS NULL OR retry_until > first_attempt_at)
);

ALTER TABLE adcp_task_webhook_outbox
    ADD COLUMN IF NOT EXISTS signing_scope_id TEXT COLLATE "C";

CREATE INDEX IF NOT EXISTS adcp_task_webhook_outbox_work_idx
    ON adcp_task_webhook_outbox (available_at, id)
    WHERE state IN ('pending', 'in_flight');

CREATE INDEX IF NOT EXISTS adcp_task_webhook_outbox_retry_until_idx
    ON adcp_task_webhook_outbox (retry_until);
