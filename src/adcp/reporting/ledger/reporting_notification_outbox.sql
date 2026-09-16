-- #1168A: additive only; never rewrite/backfill retained ledger evidence.
-- A single DO makes direct application atomic even on an autocommit connection.
DO $outbox$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    -- Require the reviewed foundation, including the durable reconciliation feed.
    PERFORM currency FROM reporting_obligations LIMIT 0;
    PERFORM canonical_content_digest, managed_control_totals FROM reporting_revisions LIMIT 0;
    PERFORM consumer_id, max_sequence FROM reporting_reconciliation_heads LIMIT 0;
    PERFORM change_id FROM reporting_reconciliation_changes LIMIT 0;

    CREATE TABLE IF NOT EXISTS reporting_notification_events (
        account_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        notification_type TEXT COLLATE "C" NOT NULL CHECK
            (notification_type IN ('reporting.ledger_changed', 'reporting.delivery_ready')),
        cause_kind TEXT COLLATE "C" NOT NULL CHECK
            (cause_kind IN ('revision_published', 'adjustment_published', 'materialization_ready')),
        cause_id TEXT COLLATE "C" NOT NULL CHECK (length(cause_id) > 0),
        cause_generation BIGINT NOT NULL CHECK (cause_generation > 0),
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        fired_at TIMESTAMPTZ NOT NULL,
        snapshot JSONB NOT NULL,
        PRIMARY KEY (account_id, consumer_namespace, notification_id),
        UNIQUE (account_id, consumer_namespace, notification_type,
                cause_kind, cause_id, cause_generation),
        CHECK ((notification_type = 'reporting.delivery_ready') =
               (cause_kind = 'materialization_ready')),
        CHECK ((cause_kind = 'materialization_ready') = (length(consumer_namespace) > 0)),
        CHECK (snapshot->>'account_id' = account_id AND
               snapshot->>'notification_id' = notification_id)
    );
    CREATE TABLE IF NOT EXISTS reporting_notification_expansions (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL DEFAULT '',
        notification_id TEXT COLLATE "C" NOT NULL,
        emission_generation BIGINT NOT NULL CHECK (emission_generation > 0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK
            (state IN ('pending', 'leased', 'complete', 'suppressed', 'quarantined')),
        due_at TIMESTAMPTZ NOT NULL,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        claim_count BIGINT NOT NULL DEFAULT 0,
        error_code TEXT CHECK (error_code IN (
            'network', 'retryable_http', 'permanent_http', 'signing_unavailable',
            'permanent_scope', 'subscription_unavailable', 'subscription_changed',
            'invalid_configuration', 'invalid_payload', 'integrity_failure', 'lease_expired')),
        PRIMARY KEY (account_id, consumer_namespace, notification_id, emission_generation),
        FOREIGN KEY (account_id, consumer_namespace, notification_id)
            REFERENCES reporting_notification_events (account_id, consumer_namespace, notification_id)
    );
    CREATE INDEX IF NOT EXISTS reporting_notification_expansions_due
        ON reporting_notification_expansions (account_id, due_at)
        WHERE state IN ('pending', 'leased');

    CREATE TABLE IF NOT EXISTS reporting_notification_deliveries (
        account_id TEXT COLLATE "C" NOT NULL,
        delivery_id TEXT COLLATE "C" NOT NULL,
        subscriber_id TEXT COLLATE "C" NOT NULL,
        principal_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        notification_type TEXT COLLATE "C" NOT NULL,
        emission_generation BIGINT NOT NULL CHECK (emission_generation > 0),
        idempotency_key TEXT COLLATE "C" NOT NULL,
        destination_sha256 TEXT NOT NULL,
        subscription_fingerprint TEXT NOT NULL,
        signing_scope_id TEXT COLLATE "C",
        cause_kind TEXT COLLATE "C" NOT NULL,
        cause_id TEXT COLLATE "C" NOT NULL,
        cause_generation BIGINT NOT NULL CHECK (cause_generation > 0),
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        auth_mode TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        envelope_version INTEGER NOT NULL,
        key_version TEXT COLLATE "C" NOT NULL,
        envelope BYTEA NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending' CHECK
            (state IN ('pending', 'leased', 'complete', 'suppressed', 'quarantined')),
        due_at TIMESTAMPTZ NOT NULL,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        claim_count BIGINT NOT NULL DEFAULT 0,
        error_code TEXT CHECK (error_code IN (
            'network', 'retryable_http', 'permanent_http', 'signing_unavailable',
            'permanent_scope', 'subscription_unavailable', 'subscription_changed',
            'invalid_configuration', 'invalid_payload', 'integrity_failure', 'lease_expired')),
        PRIMARY KEY (account_id, consumer_namespace, delivery_id),
        UNIQUE (account_id, consumer_namespace, notification_id, emission_generation, subscriber_id),
        UNIQUE (account_id, consumer_namespace, idempotency_key),
        FOREIGN KEY (account_id, consumer_namespace, notification_id, emission_generation)
            REFERENCES reporting_notification_expansions
                (account_id, consumer_namespace, notification_id, emission_generation)
    );
    CREATE INDEX IF NOT EXISTS reporting_notification_deliveries_due
        ON reporting_notification_deliveries (account_id, due_at)
        WHERE state IN ('pending', 'leased');

    CREATE TABLE IF NOT EXISTS reporting_status_dirty_heads (
        account_id TEXT COLLATE "C" PRIMARY KEY,
        max_sequence BIGINT NOT NULL CHECK (max_sequence >= 0)
    );
    CREATE TABLE IF NOT EXISTS reporting_status_dirty (
        account_id TEXT COLLATE "C" NOT NULL,
        sequence BIGINT NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        scope_sha256 TEXT NOT NULL,
        reason TEXT NOT NULL,
        cause_id TEXT COLLATE "C" NOT NULL,
        cause_generation BIGINT NOT NULL CHECK (cause_generation > 0),
        snapshot JSONB NOT NULL,
        PRIMARY KEY (account_id, sequence),
        UNIQUE (account_id, consumer_namespace, scope_sha256, reason, cause_id, cause_generation)
    );
    CREATE TABLE IF NOT EXISTS reporting_status_checkpoints (
        account_id TEXT COLLATE "C" NOT NULL,
        projector_id TEXT COLLATE "C" NOT NULL,
        sequence BIGINT NOT NULL CHECK (sequence >= 0),
        PRIMARY KEY (account_id, projector_id)
    );
    CREATE TABLE IF NOT EXISTS reporting_issue_status_scopes (
        account_id TEXT COLLATE "C" NOT NULL,
        issue_id TEXT COLLATE "C" NOT NULL,
        scope JSONB NOT NULL,
        PRIMARY KEY (account_id, issue_id)
    );

    CREATE OR REPLACE FUNCTION reporting_notification_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        RAISE EXCEPTION 'immutable reporting notification evidence' USING ERRCODE = '23514';
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgrelid = 'reporting_notification_events'::regclass
                   AND tgname = 'reporting_notification_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_notification_immutable BEFORE UPDATE OR DELETE
            ON reporting_notification_events FOR EACH ROW
            EXECUTE FUNCTION reporting_notification_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgrelid = 'reporting_status_dirty'::regclass
                   AND tgname = 'reporting_status_dirty_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_dirty_immutable BEFORE UPDATE OR DELETE
            ON reporting_status_dirty FOR EACH ROW
            EXECUTE FUNCTION reporting_notification_immutable();
    END IF;
END
$outbox$;
