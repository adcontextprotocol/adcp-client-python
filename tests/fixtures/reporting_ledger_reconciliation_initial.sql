-- Storage foundation for #1167. Apply after the account-generation and currency
-- migrations, with old writers drained. This single DO is atomic in autocommit.
-- No legacy evidence is inferred, hashed again, or backfilled.
DO $reconciliation$
DECLARE
    evidence_type OID;
    evidence_default TEXT;
    evidence_nullable BOOLEAN;
    evidence_table TEXT;
    evidence_column TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    -- Resolve prerequisite columns/keys before installing any extension tables.
    PERFORM currency FROM reporting_obligations LIMIT 0;
    ALTER TABLE reporting_revisions ADD COLUMN IF NOT EXISTS canonical_content_digest JSONB;
    ALTER TABLE reporting_revisions ADD COLUMN IF NOT EXISTS managed_control_totals JSONB;
    ALTER TABLE reporting_adjustments ADD COLUMN IF NOT EXISTS managed_control_total_deltas JSONB;
    FOR evidence_table, evidence_column IN VALUES
        ('reporting_revisions', 'canonical_content_digest'),
        ('reporting_revisions', 'managed_control_totals'),
        ('reporting_adjustments', 'managed_control_total_deltas')
    LOOP
        SELECT a.atttypid, pg_get_expr(d.adbin, d.adrelid), NOT a.attnotnull
        INTO evidence_type, evidence_default, evidence_nullable
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = evidence_table::regclass AND a.attname = evidence_column;
        IF evidence_type <> 'jsonb'::regtype OR evidence_default IS NOT NULL OR NOT evidence_nullable THEN
            RAISE EXCEPTION 'Unexpected reporting evidence column';
        END IF;
    END LOOP;

    CREATE OR REPLACE FUNCTION reporting_canonical_evidence_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        IF NEW.canonical_content_digest IS DISTINCT FROM OLD.canonical_content_digest
           OR NEW.managed_control_totals IS DISTINCT FROM OLD.managed_control_totals THEN
            RAISE EXCEPTION 'reporting canonical evidence is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_revisions'::regclass
                   AND tgname = 'reporting_canonical_evidence_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_canonical_evidence_immutable
            BEFORE UPDATE OF canonical_content_digest, managed_control_totals ON reporting_revisions
            FOR EACH ROW EXECUTE FUNCTION reporting_canonical_evidence_immutable();
    END IF;

    CREATE OR REPLACE FUNCTION reporting_adjustment_evidence_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        IF NEW.managed_control_total_deltas IS DISTINCT FROM OLD.managed_control_total_deltas THEN
            RAISE EXCEPTION 'reporting adjustment evidence is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_adjustments'::regclass
                   AND tgname = 'reporting_adjustment_evidence_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_adjustment_evidence_immutable
            BEFORE UPDATE OF managed_control_total_deltas ON reporting_adjustments
            FOR EACH ROW EXECUTE FUNCTION reporting_adjustment_evidence_immutable();
    END IF;

    -- The older Core identifiers remain globally unique. Every new relationship
    -- nevertheless uses the account in its foreign key; a global ID is no grant.
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_obligations_account_identity
        ON reporting_obligations(account_id, reporting_obligation_id);
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_revisions_account_identity
        ON reporting_revisions(account_id, reporting_revision_id);
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_adjustments_account_identity
        ON reporting_adjustments(account_id, reporting_adjustment_id);

    -- Each payload is the SDK's closed, frozen record shape, not a generated wire
    -- object or provider response. Identity/join/transition fields are explicit.
    CREATE TABLE IF NOT EXISTS reporting_reconciliation_records (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        namespace TEXT COLLATE "C" NOT NULL,
        record_id TEXT COLLATE "C" NOT NULL,
        record_kind TEXT NOT NULL CHECK (record_kind IN (
            'destination_binding', 'obligation_delivery', 'materialization_attempt',
            'materialization', 'materialization_check', 'revision_receipt', 'adjustment_receipt')),
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version INTEGER NOT NULL,
        reporting_obligation_id TEXT COLLATE "C",
        reporting_revision_id TEXT COLLATE "C",
        reporting_materialization_id TEXT COLLATE "C",
        reporting_adjustment_id TEXT COLLATE "C",
        attempt_number INTEGER CHECK (attempt_number > 0),
        receipt_chain_key TEXT COLLATE "C",
        receipt_status TEXT CHECK (receipt_status IN ('accepted', 'rejected')),
        supersedes_receipt_id TEXT COLLATE "C",
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        content_sha256 TEXT COLLATE "C" NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
        change_id TEXT COLLATE "C" NOT NULL,
        PRIMARY KEY (account_id, consumer_id, namespace, record_id),
        UNIQUE (account_id, record_kind, change_id),
        CHECK (namespace = CASE WHEN record_kind IN ('revision_receipt', 'adjustment_receipt')
                               THEN 'receipt' ELSE record_kind END),
        CHECK ((record_kind IN ('revision_receipt', 'adjustment_receipt')) = (receipt_status IS NOT NULL)),
        CHECK ((receipt_status IS NOT NULL) = (receipt_chain_key IS NOT NULL)),
        CHECK ((record_kind = 'materialization_attempt') = (attempt_number IS NOT NULL)),
        CHECK (payload->>'kind' = record_kind),
        FOREIGN KEY (account_id, delivery_config_id, delivery_config_version)
            REFERENCES reporting_configurations(account_id, delivery_config_id, delivery_config_version),
        FOREIGN KEY (account_id, reporting_obligation_id)
            REFERENCES reporting_obligations(account_id, reporting_obligation_id),
        FOREIGN KEY (account_id, reporting_revision_id)
            REFERENCES reporting_revisions(account_id, reporting_revision_id),
        FOREIGN KEY (account_id, reporting_adjustment_id)
            REFERENCES reporting_adjustments(account_id, reporting_adjustment_id),
        FOREIGN KEY (account_id, consumer_id, namespace, supersedes_receipt_id)
            REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_materialization_attempt_identity
        ON reporting_reconciliation_records(account_id, consumer_id, reporting_obligation_id,
                                             reporting_revision_id, attempt_number)
        WHERE record_kind = 'materialization_attempt';
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_receipt_one_successor
        ON reporting_reconciliation_records(account_id, consumer_id, supersedes_receipt_id)
        WHERE supersedes_receipt_id IS NOT NULL;

    CREATE TABLE IF NOT EXISTS reporting_receipt_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        chain_key TEXT COLLATE "C" NOT NULL,
        namespace TEXT NOT NULL DEFAULT 'receipt' CHECK (namespace = 'receipt'),
        receipt_id TEXT COLLATE "C" NOT NULL,
        receipt_status TEXT NOT NULL CHECK (receipt_status IN ('accepted', 'rejected')),
        supersedes_receipt_id TEXT COLLATE "C",
        PRIMARY KEY (account_id, consumer_id, chain_key),
        FOREIGN KEY (account_id, consumer_id, namespace, receipt_id)
            REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id)
    );

    CREATE OR REPLACE FUNCTION reporting_reconciliation_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        RAISE EXCEPTION 'reporting reconciliation evidence is append-only' USING ERRCODE = '23514';
    END
    $function$;
    CREATE OR REPLACE FUNCTION reporting_receipt_terminal()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP = 'DELETE' OR OLD.receipt_status = 'accepted'
           OR NEW.account_id <> OLD.account_id OR NEW.consumer_id <> OLD.consumer_id
           OR NEW.chain_key <> OLD.chain_key
           OR NEW.supersedes_receipt_id IS DISTINCT FROM OLD.receipt_id THEN
            RAISE EXCEPTION 'reporting receipt replacement is invalid' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_reconciliation_records'::regclass
                   AND tgname = 'reporting_reconciliation_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_reconciliation_immutable
            BEFORE UPDATE OR DELETE ON reporting_reconciliation_records
            FOR EACH ROW EXECUTE FUNCTION reporting_reconciliation_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_receipt_heads'::regclass
                   AND tgname = 'reporting_receipt_terminal' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_receipt_terminal
            BEFORE UPDATE OR DELETE ON reporting_receipt_heads
            FOR EACH ROW EXECUTE FUNCTION reporting_receipt_terminal();
    END IF;
END
$reconciliation$;
