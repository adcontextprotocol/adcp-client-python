-- B2.2 additive receipt ingress. No older manifest, decoder, or queue is replaced.
-- Execute in the caller's transaction; an interrupted migration leaves no partial feature.
DO $migration$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.receipt_ingestion.schema'));

    -- Whole receipt requests may have arbitrary Unicode context/ext keys.
    -- JCS sorts UTF-16 code units, which differs from the older closed-record
    -- encoder's C ordering for supplementary characters. Keep this additive;
    -- replacing reporting_payload_sha256 would invalidate approved manifests.
    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_utf16(value TEXT)
    RETURNS BYTEA LANGUAGE plpgsql IMMUTABLE STRICT AS $function$
    DECLARE
        result BYTEA := ''::bytea;
        point INTEGER;
        i INTEGER;
    BEGIN
        FOR i IN 1..char_length(value) LOOP
            point := ascii(substr(value,i,1));
            IF point > 65535 THEN
                point := point - 65536;
                result := result || decode(lpad(to_hex(55296 + (point >> 10)),4,'0')
                    || lpad(to_hex(56320 + (point & 1023)),4,'0'),'hex');
            ELSE
                result := result || decode(lpad(to_hex(point),4,'0'),'hex');
            END IF;
        END LOOP;
        RETURN result;
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_canonical(document JSONB)
    RETURNS TEXT LANGUAGE plpgsql IMMUTABLE STRICT AS $function$
    DECLARE
        shape TEXT := jsonb_typeof(document);
        parts TEXT;
    BEGIN
        IF shape = 'object' THEN
            SELECT coalesce(string_agg(to_json(e.key)::text || ':'
                || reporting_receipt_ingestion_canonical(e.value), ','
                ORDER BY reporting_receipt_ingestion_utf16(e.key)), '') INTO parts
            FROM jsonb_each(document) e;
            RETURN '{' || parts || '}';
        ELSIF shape = 'array' THEN
            SELECT coalesce(string_agg(reporting_receipt_ingestion_canonical(e.value), ','
                ORDER BY e.ordinality), '') INTO parts
            FROM jsonb_array_elements(document) WITH ORDINALITY e;
            RETURN '[' || parts || ']';
        END IF;
        -- The approved scalar rules already match this restricted JCS profile.
        RETURN reporting_canonical_json(document);
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_sha256(document JSONB)
    RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $function$
        SELECT encode(sha256(convert_to(reporting_receipt_ingestion_canonical(document), 'UTF8')), 'hex')
    $function$;

    CREATE TABLE IF NOT EXISTS reporting_receipt_ingestion_batches (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9_.:-]{16,255}$'),
        canonical_request TEXT NOT NULL,
        request_sha256 TEXT COLLATE "C" NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
        expected_count INTEGER NOT NULL CHECK (expected_count BETWEEN 1 AND 100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        final_response JSONB,
        final_sha256 TEXT COLLATE "C",
        finalized_at TIMESTAMPTZ,
        admission_epoch BIGINT NOT NULL DEFAULT 0 CHECK (admission_epoch = 0),
        PRIMARY KEY (account_id, consumer_id, idempotency_key),
        CHECK (request_sha256 = encode(sha256(convert_to(canonical_request, 'UTF8')), 'hex')),
        CHECK (request_sha256 = reporting_receipt_ingestion_sha256(canonical_request::jsonb)),
        CHECK (jsonb_typeof(canonical_request::jsonb) = 'object'),
        CHECK ((canonical_request::jsonb->>'idempotency_key') IS NOT DISTINCT FROM idempotency_key),
        CHECK ((final_response IS NULL) = (final_sha256 IS NULL)),
        CHECK ((final_response IS NULL) = (finalized_at IS NULL)),
        CHECK (final_response IS NULL OR final_sha256 = reporting_receipt_ingestion_sha256(final_response))
    );

    CREATE TABLE IF NOT EXISTS reporting_receipt_ingestion_results (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 99),
        receipt_kind TEXT NOT NULL CHECK (receipt_kind IN ('revision_receipt','adjustment_receipt')),
        reporting_receipt_id TEXT COLLATE "C" NOT NULL CHECK (reporting_receipt_id ~ '^[A-Za-z0-9_.:-]{16,255}$'),
        receipt_namespace TEXT COLLATE "C" NOT NULL DEFAULT 'receipt' CHECK (receipt_namespace = 'receipt'),
        receipt_record_id TEXT COLLATE "C",
        result JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id, consumer_id, idempotency_key, ordinal),
        UNIQUE (account_id, consumer_id, idempotency_key, reporting_receipt_id),
        FOREIGN KEY (account_id, consumer_id, idempotency_key) REFERENCES reporting_receipt_ingestion_batches,
        FOREIGN KEY (account_id, consumer_id, receipt_namespace, receipt_record_id)
            REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id),
        CHECK (content_sha256 = reporting_receipt_ingestion_sha256(result)),
        CHECK (result ? 'result' AND result->>'result' IS NOT NULL
            AND result->>'result' IN ('recorded','unchanged','failed')),
        CHECK ((result->>'result' = 'failed') = (receipt_record_id IS NULL)),
        CHECK (receipt_record_id IS NULL OR receipt_record_id = reporting_receipt_id)
    );

    CREATE TABLE IF NOT EXISTS reporting_receipt_ingestion_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        max_sequence BIGINT NOT NULL CHECK (max_sequence > 0),
        PRIMARY KEY (account_id, consumer_id)
    );
    CREATE INDEX IF NOT EXISTS reporting_receipt_ingestion_chain
        ON reporting_reconciliation_records(account_id, consumer_id, receipt_chain_key)
        WHERE namespace = 'receipt';
    CREATE TABLE IF NOT EXISTS reporting_receipt_ingestion_boundaries (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        sequence BIGINT NOT NULL CHECK (sequence > 0),
        account_sequence BIGINT NOT NULL CHECK (account_sequence > 0),
        reporting_receipt_id TEXT COLLATE "C" NOT NULL,
        receipt_namespace TEXT COLLATE "C" NOT NULL DEFAULT 'receipt' CHECK (receipt_namespace = 'receipt'),
        as_of TIMESTAMPTZ NOT NULL,
        input JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        PRIMARY KEY (account_id, consumer_id, sequence),
        UNIQUE (account_id, consumer_id, reporting_receipt_id),
        UNIQUE (account_id, account_sequence),
        FOREIGN KEY (account_id, consumer_id, receipt_namespace, reporting_receipt_id)
            REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id),
        CHECK (content_sha256 = reporting_receipt_ingestion_sha256(input)),
        CHECK ((input->>'version')::integer IS NOT DISTINCT FROM 1),
        CHECK ((input->>'admission_epoch')::bigint IS NOT DISTINCT FROM 0),
        CHECK ((input->>'account_id') IS NOT DISTINCT FROM account_id),
        CHECK ((input->>'consumer_id') IS NOT DISTINCT FROM consumer_id),
        CHECK ((input->>'reporting_receipt_id') IS NOT DISTINCT FROM reporting_receipt_id),
        CHECK ((input->>'sequence')::bigint IS NOT DISTINCT FROM sequence),
        CHECK ((input->>'account_sequence')::bigint IS NOT DISTINCT FROM account_sequence),
        CHECK ((input->>'as_of')::timestamptz IS NOT DISTINCT FROM as_of),
        CHECK (input - ARRAY['version','admission_epoch','account_id','consumer_id',
            'sequence','account_sequence','reporting_receipt_id','as_of','core','reconciliation'] = '{}'::jsonb)
    );

    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        RAISE EXCEPTION 'receipt ingestion evidence is immutable' USING ERRCODE = '23514';
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_batch_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    DECLARE
        request JSONB;
        revisions JSONB;
        adjustments JSONB;
        expected JSONB;
        n INTEGER;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'receipt batch is immutable' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        IF TG_OP = 'UPDATE' THEN
            IF (to_jsonb(NEW) - ARRAY['final_response','final_sha256','finalized_at'])
               IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['final_response','final_sha256','finalized_at'])
               OR OLD.final_response IS NOT NULL OR NEW.final_response IS NULL THEN
                RAISE EXCEPTION 'receipt batch is immutable' USING ERRCODE = '23514';
            END IF;
            SELECT count(*), jsonb_agg(r.result ORDER BY r.ordinal) INTO n, expected
            FROM reporting_receipt_ingestion_results r
            WHERE (r.account_id,r.consumer_id,r.idempotency_key)
                = (NEW.account_id,NEW.consumer_id,NEW.idempotency_key);
            IF n <> NEW.expected_count OR NEW.final_response->'results' IS DISTINCT FROM expected
               OR NEW.final_response->>'status' IS DISTINCT FROM 'completed' THEN
                RAISE EXCEPTION 'receipt batch results are incomplete' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END IF;
        request := NEW.canonical_request::jsonb;
        revisions := coalesce(request->'receipts','[]'::jsonb);
        adjustments := coalesce(request->'adjustment_receipts','[]'::jsonb);
        IF jsonb_typeof(revisions) <> 'array' OR jsonb_typeof(adjustments) <> 'array'
           OR (request ? 'receipts' AND jsonb_array_length(revisions) = 0)
           OR (request ? 'adjustment_receipts' AND jsonb_array_length(adjustments) = 0)
           OR jsonb_array_length(revisions) + jsonb_array_length(adjustments) <> NEW.expected_count
           OR EXISTS (SELECT 1 FROM jsonb_array_elements(revisions || adjustments) i WHERE i ? 'received_at')
           OR (SELECT count(DISTINCT i->>'reporting_receipt_id')
               FROM jsonb_array_elements(revisions || adjustments) i) <> NEW.expected_count
           OR NEW.final_response IS NOT NULL THEN
            RAISE EXCEPTION 'receipt batch shape is invalid' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_wire(document JSONB)
    RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $function$
        SELECT jsonb_strip_nulls(document - ARRAY['scope','kind','rejection_codes'])
            || CASE WHEN document->>'kind' = 'revision_receipt'
                THEN jsonb_build_object('reporting_obligation_id',document#>>'{scope,reporting_obligation_id}')
                ELSE '{}'::jsonb END
            || CASE WHEN jsonb_array_length(document->'rejection_codes') > 0
                THEN jsonb_build_object('rejection_codes',document->'rejection_codes') ELSE '{}'::jsonb END
            || CASE WHEN jsonb_typeof(document->'observed_canonical_content_digest') = 'object'
                THEN jsonb_build_object('observed_canonical_content_digest',
                    document->'observed_canonical_content_digest' || '{"algorithm":"sha256"}'::jsonb)
                ELSE '{}'::jsonb END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_result_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    DECLARE
        batch reporting_receipt_ingestion_batches;
        request JSONB;
        item JSONB;
        expected_kind TEXT;
        wire_key TEXT;
        evidence JSONB;
        n INTEGER;
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        SELECT * INTO batch FROM reporting_receipt_ingestion_batches b
        WHERE (b.account_id,b.consumer_id,b.idempotency_key)
            = (NEW.account_id,NEW.consumer_id,NEW.idempotency_key) FOR UPDATE;
        SELECT count(*) INTO n FROM reporting_receipt_ingestion_results r
        WHERE (r.account_id,r.consumer_id,r.idempotency_key)
            = (NEW.account_id,NEW.consumer_id,NEW.idempotency_key);
        IF batch.idempotency_key IS NULL OR batch.final_response IS NOT NULL
           OR NEW.ordinal <> n OR NEW.ordinal >= batch.expected_count THEN
            RAISE EXCEPTION 'receipt ordinal is unavailable' USING ERRCODE = '23514';
        END IF;
        request := batch.canonical_request::jsonb;
        n := jsonb_array_length(coalesce(request->'receipts','[]'::jsonb));
        IF NEW.ordinal < n THEN
            expected_kind := 'revision_receipt'; wire_key := 'receipt';
            item := request->'receipts'->NEW.ordinal;
        ELSE
            expected_kind := 'adjustment_receipt'; wire_key := 'adjustment_receipt';
            item := request->'adjustment_receipts'->(NEW.ordinal - n);
        END IF;
        IF NEW.receipt_kind <> expected_kind
           OR NEW.reporting_receipt_id IS DISTINCT FROM item->>'reporting_receipt_id' THEN
            RAISE EXCEPTION 'receipt ordinal identity differs' USING ERRCODE = '23514';
        END IF;
        IF NEW.result->>'result' = 'failed' THEN
            IF NEW.result - ARRAY['result','reporting_receipt_id','errors'] <> '{}'::jsonb
               OR NEW.result->>'reporting_receipt_id' IS DISTINCT FROM NEW.reporting_receipt_id
               OR jsonb_typeof(NEW.result->'errors') IS DISTINCT FROM 'array'
               OR jsonb_array_length(NEW.result->'errors') NOT BETWEEN 1 AND 16 THEN
                RAISE EXCEPTION 'receipt failure shape differs' USING ERRCODE = '23514';
            END IF;
        ELSE
            SELECT reporting_receipt_ingestion_wire(r.payload) INTO evidence
            FROM reporting_reconciliation_records r
            WHERE (r.account_id,r.consumer_id,r.namespace,r.record_id,r.record_kind)
                = (NEW.account_id,NEW.consumer_id,'receipt',NEW.reporting_receipt_id,NEW.receipt_kind);
            IF evidence IS NULL OR NEW.result->wire_key IS DISTINCT FROM evidence
               OR NEW.result - ARRAY['result',wire_key] <> '{}'::jsonb THEN
                RAISE EXCEPTION 'receipt result evidence differs' USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END
    $function$;

    -- Additional write predicates leave all approved reconciliation predicates
    -- unchanged. They apply to raw SQL too, and only to newly written receipts.
    CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_graph()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    DECLARE
        revision reporting_revisions;
        obligation reporting_obligations;
        adjustment reporting_adjustments;
        outcome JSONB;
        observed TIMESTAMPTZ;
        received TIMESTAMPTZ;
        check_state TEXT;
        chain_count INTEGER;
        leaf_count INTEGER;
        predecessor reporting_reconciliation_records;
        visited TEXT[] := ARRAY[]::TEXT[];
    BEGIN
        IF NEW.record_kind NOT IN ('revision_receipt','adjustment_receipt') THEN RETURN NEW; END IF;
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        observed := (NEW.payload->>'observed_at')::timestamptz;
        received := (NEW.payload->>'received_at')::timestamptz;
        IF observed IS NULL OR received IS NULL OR observed > received OR received > clock_timestamp()
           OR NOT EXISTS (SELECT 1 FROM reporting_reconciliation_records b
               WHERE (b.account_id,b.consumer_id,b.delivery_config_id,b.delivery_config_version)
                   = (NEW.account_id,NEW.consumer_id,NEW.delivery_config_id,NEW.delivery_config_version)
                 AND b.record_kind='destination_binding' AND b.payload->>'reconciliation_mode'='consumer_receipt') THEN
            RAISE EXCEPTION 'receipt target is unavailable' USING ERRCODE = '23514';
        END IF;
        SELECT * INTO revision FROM reporting_revisions v
        WHERE (v.account_id,v.reporting_obligation_id,v.reporting_revision_id)
            = (NEW.account_id,NEW.reporting_obligation_id,NEW.reporting_revision_id);
        SELECT * INTO obligation FROM reporting_obligations o
        WHERE (o.account_id,o.reporting_obligation_id,o.delivery_config_id,o.delivery_config_version)
            = (NEW.account_id,NEW.reporting_obligation_id,NEW.delivery_config_id,NEW.delivery_config_version);
        IF revision.reporting_revision_id IS NULL OR obligation.reporting_obligation_id IS NULL THEN
            RAISE EXCEPTION 'receipt target is unavailable' USING ERRCODE = '23514';
        END IF;
        SELECT count(*) INTO chain_count FROM reporting_reconciliation_records r
        WHERE (r.account_id,r.consumer_id,r.namespace,r.receipt_chain_key)
            = (NEW.account_id,NEW.consumer_id,'receipt',NEW.receipt_chain_key);
        IF chain_count > 0 THEN
            SELECT count(*) INTO leaf_count FROM reporting_reconciliation_records r
            WHERE (r.account_id,r.consumer_id,r.namespace,r.receipt_chain_key)
                = (NEW.account_id,NEW.consumer_id,'receipt',NEW.receipt_chain_key)
              AND NOT EXISTS (SELECT 1 FROM reporting_reconciliation_records s
                  WHERE (s.account_id,s.consumer_id,s.namespace,s.supersedes_receipt_id)
                      = (r.account_id,r.consumer_id,'receipt',r.record_id));
            SELECT r.* INTO predecessor FROM reporting_reconciliation_records r
            WHERE (r.account_id,r.consumer_id,r.namespace,r.record_id)
                = (NEW.account_id,NEW.consumer_id,'receipt',NEW.supersedes_receipt_id);
            IF leaf_count <> 1 OR predecessor.record_id IS NULL THEN
                RAISE EXCEPTION 'receipt replacement is unavailable' USING ERRCODE = '23514';
            END IF;
            LOOP
                IF predecessor.record_id IS NULL OR predecessor.record_id = ANY(visited)
                   OR predecessor.receipt_status <> 'rejected'
                   OR (predecessor.record_kind,predecessor.delivery_config_id,
                       predecessor.delivery_config_version,predecessor.reporting_obligation_id,
                       predecessor.reporting_revision_id,predecessor.receipt_chain_key)
                       IS DISTINCT FROM (NEW.record_kind,NEW.delivery_config_id,
                       NEW.delivery_config_version,NEW.reporting_obligation_id,
                       NEW.reporting_revision_id,NEW.receipt_chain_key)
                   OR predecessor.reporting_adjustment_id IS DISTINCT FROM NEW.reporting_adjustment_id THEN
                    RAISE EXCEPTION 'receipt history is inconsistent' USING ERRCODE = '23514';
                END IF;
                visited := array_append(visited, predecessor.record_id);
                EXIT WHEN predecessor.supersedes_receipt_id IS NULL;
                SELECT r.* INTO predecessor FROM reporting_reconciliation_records r
                WHERE (r.account_id,r.consumer_id,r.namespace,r.record_id)
                    = (NEW.account_id,NEW.consumer_id,'receipt',predecessor.supersedes_receipt_id);
            END LOOP;
            IF cardinality(visited) <> chain_count THEN
                RAISE EXCEPTION 'receipt history is inconsistent' USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.supersedes_receipt_id IS NOT NULL THEN
            RAISE EXCEPTION 'receipt replacement is unavailable' USING ERRCODE = '23514';
        END IF;
        IF NEW.record_kind='adjustment_receipt' THEN
            SELECT * INTO adjustment FROM reporting_adjustments a
            WHERE (a.account_id,a.adjusts_reporting_revision_id,a.reporting_adjustment_id)
                = (NEW.account_id,NEW.reporting_revision_id,NEW.reporting_adjustment_id);
            IF adjustment.reporting_adjustment_id IS NULL OR revision.finality <> 'official'
               OR revision.finalized_at IS NULL
               OR NOT (obligation.period_end <= revision.finalized_at
                   AND revision.finalized_at <= revision.created_at
                   AND revision.finalized_at <= adjustment.correction_observed_at
                   AND adjustment.correction_observed_at <= adjustment.created_at
                   AND adjustment.accounting_period_start < adjustment.accounting_period_end
                   AND adjustment.created_at <= observed) THEN
                RAISE EXCEPTION 'receipt adjustment order is invalid' USING ERRCODE = '23514';
            END IF;
        ELSE
            SELECT r.payload INTO outcome FROM reporting_reconciliation_records r
            WHERE (r.account_id,r.consumer_id,r.delivery_config_id,r.delivery_config_version,
                   r.reporting_obligation_id,r.reporting_revision_id,r.reporting_materialization_id)
                = (NEW.account_id,NEW.consumer_id,NEW.delivery_config_id,NEW.delivery_config_version,
                   NEW.reporting_obligation_id,NEW.reporting_revision_id,NEW.reporting_materialization_id)
              AND r.record_kind='materialization' AND r.payload->>'status' IN ('available','delivered');
            IF outcome IS NULL OR observed < (outcome->>'completed_at')::timestamptz
               OR NEW.payload->>'verification_profile' IS DISTINCT FROM outcome#>>'{verification,verification_profile}' THEN
                RAISE EXCEPTION 'receipt artifact is unavailable' USING ERRCODE = '23514';
            END IF;
            IF NEW.receipt_status='accepted' THEN
                SELECT r.payload->>'state' INTO check_state FROM reporting_reconciliation_records r
                WHERE r.account_id=NEW.account_id AND r.consumer_id=NEW.consumer_id
                  AND r.record_kind='materialization_check'
                  AND r.reporting_materialization_id=NEW.reporting_materialization_id
                  AND (r.payload->>'checked_at')::timestamptz <= observed
                ORDER BY (r.payload->>'checked_at')::timestamptz DESC LIMIT 1;
                IF (check_state IS NOT NULL AND check_state <> 'readable')
                   OR (NEW.payload->>'observed_row_count')::bigint
                       IS DISTINCT FROM (outcome#>>'{verification,row_count}')::bigint
                   OR (jsonb_typeof(NEW.payload->'observed_canonical_content_digest')='object'
                       AND reporting_wire_digest(NEW.payload->'observed_canonical_content_digest')
                           IS DISTINCT FROM reporting_wire_digest(outcome#>'{verification,canonical_content_digest}')) THEN
                    RAISE EXCEPTION 'receipt artifact evidence differs' USING ERRCODE = '23514';
                END IF;
            END IF;
        END IF;
        RETURN NEW;
    END
    $function$;

    DROP TRIGGER IF EXISTS reporting_receipt_ingestion_batch ON reporting_receipt_ingestion_batches;
    CREATE TRIGGER reporting_receipt_ingestion_batch BEFORE INSERT OR UPDATE OR DELETE
        ON reporting_receipt_ingestion_batches FOR EACH ROW EXECUTE FUNCTION reporting_receipt_ingestion_batch_guard();
    DROP TRIGGER IF EXISTS reporting_receipt_ingestion_result ON reporting_receipt_ingestion_results;
    CREATE TRIGGER reporting_receipt_ingestion_result BEFORE INSERT ON reporting_receipt_ingestion_results
        FOR EACH ROW EXECUTE FUNCTION reporting_receipt_ingestion_result_guard();
    DROP TRIGGER IF EXISTS reporting_receipt_ingestion_result_immutable ON reporting_receipt_ingestion_results;
    CREATE TRIGGER reporting_receipt_ingestion_result_immutable BEFORE UPDATE OR DELETE
        ON reporting_receipt_ingestion_results FOR EACH ROW EXECUTE FUNCTION reporting_receipt_ingestion_immutable();
    DROP TRIGGER IF EXISTS reporting_receipt_ingestion_boundary_immutable ON reporting_receipt_ingestion_boundaries;
    CREATE TRIGGER reporting_receipt_ingestion_boundary_immutable BEFORE UPDATE OR DELETE
        ON reporting_receipt_ingestion_boundaries FOR EACH ROW EXECUTE FUNCTION reporting_receipt_ingestion_immutable();
    DROP TRIGGER IF EXISTS reporting_receipt_ingestion_graph ON reporting_reconciliation_records;
    CREATE TRIGGER reporting_receipt_ingestion_graph BEFORE INSERT ON reporting_reconciliation_records
        FOR EACH ROW EXECUTE FUNCTION reporting_receipt_ingestion_graph();
END
$migration$;
