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
    graph_table TEXT;
    graph_constraint TEXT;
    graph_definition TEXT;
    retained_record RECORD;
    feed_installed BOOLEAN;
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

    -- Exact Core identities, including the owner of each referenced publication.
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_obligations_generation_identity
        ON reporting_obligations(account_id, delivery_config_id, delivery_config_version,
                                  reporting_obligation_id);
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_revisions_obligation_identity
        ON reporting_revisions(account_id, reporting_obligation_id, reporting_revision_id);
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_adjustments_revision_identity
        ON reporting_adjustments(account_id, adjusts_reporting_revision_id, reporting_adjustment_id);
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_receipts_exact_identity
        ON reporting_reconciliation_records(account_id, consumer_id, namespace, record_id,
                                             receipt_chain_key, receipt_status);
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_receipt_one_root
        ON reporting_reconciliation_records(account_id, consumer_id, receipt_chain_key)
        WHERE receipt_status IS NOT NULL AND supersedes_receipt_id IS NULL;
    CREATE INDEX IF NOT EXISTS reporting_reconciliation_graph
        ON reporting_reconciliation_records(account_id, consumer_id, record_kind,
                                             delivery_config_id, delivery_config_version,
                                             reporting_obligation_id, reporting_materialization_id);

    -- ADD CONSTRAINT validates existing history as well as future SQL writes.
    -- Keep existing constraint OIDs on repeated/concurrent upgrades.
    FOR graph_table, graph_constraint, graph_definition IN VALUES
        ('reporting_revisions', 'reporting_revision_exact_obligation',
         'FOREIGN KEY (account_id, reporting_obligation_id) REFERENCES reporting_obligations(account_id, reporting_obligation_id)'),
        ('reporting_revisions', 'reporting_revision_exact_predecessor',
         'FOREIGN KEY (account_id, reporting_obligation_id, supersedes_reporting_revision_id) REFERENCES reporting_revisions(account_id, reporting_obligation_id, reporting_revision_id)'),
        ('reporting_adjustments', 'reporting_adjustment_exact_revision',
         'FOREIGN KEY (account_id, adjusts_reporting_revision_id) REFERENCES reporting_revisions(account_id, reporting_revision_id)'),
        ('reporting_reconciliation_records', 'reporting_record_exact_obligation',
         'FOREIGN KEY (account_id, delivery_config_id, delivery_config_version, reporting_obligation_id) REFERENCES reporting_obligations(account_id, delivery_config_id, delivery_config_version, reporting_obligation_id)'),
        ('reporting_reconciliation_records', 'reporting_record_exact_revision',
         'FOREIGN KEY (account_id, reporting_obligation_id, reporting_revision_id) REFERENCES reporting_revisions(account_id, reporting_obligation_id, reporting_revision_id)'),
        ('reporting_reconciliation_records', 'reporting_record_exact_adjustment',
         'FOREIGN KEY (account_id, reporting_revision_id, reporting_adjustment_id) REFERENCES reporting_adjustments(account_id, adjusts_reporting_revision_id, reporting_adjustment_id)'),
        ('reporting_reconciliation_records', 'reporting_record_identity_shape',
         $shape$CHECK (
            (record_kind = 'destination_binding') = (reporting_obligation_id IS NULL)
            AND (record_kind IN ('materialization_attempt', 'materialization', 'revision_receipt', 'adjustment_receipt')) = (reporting_revision_id IS NOT NULL)
            AND (record_kind IN ('materialization_attempt', 'materialization', 'materialization_check', 'revision_receipt')) = (reporting_materialization_id IS NOT NULL)
            AND (record_kind = 'adjustment_receipt') = (reporting_adjustment_id IS NOT NULL)
            AND (supersedes_receipt_id IS NULL OR receipt_status IS NOT NULL)
            AND (supersedes_receipt_id IS NULL OR supersedes_receipt_id <> record_id)
         )$shape$),
        ('reporting_receipt_heads', 'reporting_head_exact_receipt',
         'FOREIGN KEY (account_id, consumer_id, namespace, receipt_id, chain_key, receipt_status) REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id, receipt_chain_key, receipt_status)')
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = graph_table::regclass
                       AND conname = graph_constraint) THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s',
                           graph_table, graph_constraint, graph_definition);
        END IF;
    END LOOP;

    CREATE OR REPLACE FUNCTION reporting_identity_sha256(value TEXT)
    RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $function$
        SELECT encode(sha256(convert_to(value, 'UTF8')), 'hex')
    $function$;

    -- canonical_json_utf8_v1 for the restricted value domain these payloads use:
    -- objects, arrays, strings, safe integers, booleans and null. Keys sort by
    -- bytes, which equals the JCS UTF-16 order for the ASCII field names the SDK
    -- emits; anything else simply fails the digest instead of being blessed.
    CREATE OR REPLACE FUNCTION reporting_canonical_json(document JSONB)
    RETURNS TEXT LANGUAGE plpgsql IMMUTABLE STRICT AS $function$
    DECLARE
        shape TEXT := jsonb_typeof(document);
        parts TEXT;
        quantity NUMERIC;
    BEGIN
        IF shape = 'object' THEN
            SELECT coalesce(string_agg(to_json(entry.field)::text || ':'
                || reporting_canonical_json(entry.nested), ',' ORDER BY entry.field COLLATE "C"), '')
            INTO parts
            FROM (SELECT e.key AS field, e.value AS nested FROM jsonb_each(document) AS e) AS entry;
            RETURN '{' || parts || '}';
        ELSIF shape = 'array' THEN
            SELECT coalesce(string_agg(reporting_canonical_json(entry.nested), ','
                ORDER BY entry.position), '')
            INTO parts
            FROM (SELECT e.value AS nested, e.ordinality AS position
                  FROM jsonb_array_elements(document) WITH ORDINALITY AS e(value, ordinality)) AS entry;
            RETURN '[' || parts || ']';
        ELSIF shape = 'string' THEN
            RETURN to_json(document #>> '{}')::text;
        ELSIF shape = 'number' THEN
            quantity := (document #>> '{}')::numeric;
            -- Outside the safe-integer domain canonical_json_utf8_v1 refuses to
            -- encode at all, so emit the raw text: the digest then cannot match and
            -- the row is refused as inconsistent instead of raising from a read.
            IF quantity = trunc(quantity) AND abs(quantity) <= 9007199254740991 THEN
                RETURN trunc(quantity)::bigint::text;
            END IF;
            RETURN document #>> '{}';
        ELSIF shape = 'boolean' THEN
            RETURN CASE WHEN (document #>> '{}')::boolean THEN 'true' ELSE 'false' END;
        END IF;
        RETURN 'null';
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_payload_sha256(document JSONB)
    RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $function$
        SELECT encode(sha256(convert_to(reporting_canonical_json(document), 'UTF8')), 'hex')
    $function$;

    -- Every object key at any depth, so a closed allowlist can refuse arbitrary
    -- retained metadata without enumerating one schema per record kind.
    CREATE OR REPLACE FUNCTION reporting_payload_keys(document JSONB)
    RETURNS SETOF TEXT LANGUAGE plpgsql IMMUTABLE STRICT AS $function$
    DECLARE
        entry RECORD;
    BEGIN
        IF jsonb_typeof(document) = 'object' THEN
            FOR entry IN SELECT e.key AS field, e.value AS nested FROM jsonb_each(document) AS e LOOP
                RETURN NEXT entry.field;
                RETURN QUERY SELECT reporting_payload_keys(entry.nested);
            END LOOP;
        ELSIF jsonb_typeof(document) = 'array' THEN
            FOR entry IN SELECT e.value AS nested FROM jsonb_array_elements(document) AS e LOOP
                RETURN QUERY SELECT reporting_payload_keys(entry.nested);
            END LOOP;
        END IF;
        RETURN;
    END
    $function$;

    -- datetime.isoformat() with '+00:00' spelled 'Z', so a recomputed adjustment
    -- digest agrees with the SDK byte-for-byte.
    CREATE OR REPLACE FUNCTION reporting_iso_utc(moment TIMESTAMPTZ)
    RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $function$
        SELECT to_char(moment AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
            || CASE WHEN (date_part('microsecond', moment AT TIME ZONE 'UTC')::bigint % 1000000) = 0
                    THEN ''
                    ELSE '.' || lpad((date_part('microsecond', moment AT TIME ZONE 'UTC')::bigint
                                      % 1000000)::text, 6, '0')
               END || 'Z'
    $function$;

    -- A retained column holds the wire projection (absent unit, explicit
    -- algorithm); a record payload holds the dataclass projection (null unit, no
    -- algorithm). Compare the facts they share rather than their spellings.
    CREATE OR REPLACE FUNCTION reporting_sorted_totals(document JSONB)
    RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $function$
        SELECT coalesce((SELECT jsonb_agg(t ORDER BY t->>'name')
                         FROM jsonb_array_elements(jsonb_strip_nulls(document)) AS t), '[]'::jsonb)
    $function$;

    CREATE OR REPLACE FUNCTION reporting_wire_digest(document JSONB)
    RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $function$
        SELECT CASE WHEN jsonb_typeof(document) = 'object'
                    THEN jsonb_strip_nulls(document) - 'algorithm' END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_sorted_strings(document JSONB)
    RETURNS JSONB LANGUAGE SQL IMMUTABLE AS $function$
        SELECT coalesce((SELECT jsonb_agg(DISTINCT s ORDER BY s)
                         FROM jsonb_array_elements_text(document) AS s), '[]'::jsonb)
    $function$;

    -- Financial acceptance cannot rest on Python pre-insert checks alone: a
    -- coherent dataclass payload with a correct fingerprint would otherwise let
    -- ordinary direct SQL commit a successful materialization or an accepted
    -- terminal receipt whose totals, digest, format or evidence disagree with the
    -- Core revision it claims. These predicates mirror _verify_materialization
    -- and _verify_receipt for exactly the content that decides money.
    CREATE OR REPLACE FUNCTION reporting_reconciliation_evidence(r reporting_reconciliation_records)
    RETURNS VOID LANGUAGE plpgsql AS $function$
    DECLARE
        binding JSONB;
        delivery JSONB;
        outcome JSONB;
        expected_rows BIGINT;
        expected_totals JSONB;
        expected_digest JSONB;
        observed_digest JSONB;
        adjustment reporting_adjustments;
    BEGIN
        IF r.record_kind NOT IN ('materialization', 'revision_receipt', 'adjustment_receipt')
           OR (r.record_kind = 'materialization'
               AND r.payload->>'status' NOT IN ('available', 'delivered'))
           OR (r.receipt_status IS NOT NULL AND r.receipt_status <> 'accepted') THEN
            RETURN;
        END IF;
        SELECT v.row_count, v.managed_control_totals, v.canonical_content_digest
        INTO expected_rows, expected_totals, expected_digest
        FROM reporting_revisions v
        WHERE (v.account_id, v.reporting_obligation_id, v.reporting_revision_id)
            = (r.account_id, r.reporting_obligation_id, r.reporting_revision_id);
        IF r.record_kind = 'adjustment_receipt' THEN
            SELECT a.* INTO adjustment FROM reporting_adjustments a
            WHERE (a.account_id, a.adjusts_reporting_revision_id, a.reporting_adjustment_id)
                = (r.account_id, r.reporting_revision_id, r.reporting_adjustment_id);
            IF adjustment.reporting_adjustment_id IS NULL
               OR adjustment.managed_control_total_deltas IS NULL
               OR jsonb_array_length(adjustment.managed_control_total_deltas) = 0
               OR r.payload->>'observed_adjustment_sha256' IS DISTINCT FROM reporting_payload_sha256(
                    jsonb_build_object(
                        'reporting_adjustment_id', adjustment.reporting_adjustment_id,
                        'adjusts_reporting_revision_id', adjustment.adjusts_reporting_revision_id,
                        'reason_code', adjustment.reason_code,
                        'accounting_period', jsonb_build_object(
                            'start', reporting_iso_utc(adjustment.accounting_period_start),
                            'end', reporting_iso_utc(adjustment.accounting_period_end)),
                        'control_total_deltas', adjustment.managed_control_total_deltas,
                        'correction_observed_at', reporting_iso_utc(adjustment.correction_observed_at),
                        'created_at', reporting_iso_utc(adjustment.created_at))
                    || CASE WHEN adjustment.reason_detail IS NOT NULL
                            THEN jsonb_build_object('reason_detail', adjustment.reason_detail)
                            ELSE '{}'::jsonb END) THEN
                RAISE EXCEPTION 'reporting adjustment acceptance is inconsistent' USING ERRCODE = '23514';
            END IF;
            RETURN;
        END IF;
        IF expected_totals IS NULL OR expected_rows IS NULL THEN
            RAISE EXCEPTION 'reporting revision evidence is unavailable' USING ERRCODE = '23514';
        END IF;
        IF r.record_kind = 'revision_receipt' THEN
            SELECT m.payload INTO outcome FROM reporting_reconciliation_records m
            WHERE (m.account_id, m.consumer_id, m.delivery_config_id, m.delivery_config_version,
                   m.reporting_obligation_id, m.reporting_materialization_id, m.reporting_revision_id)
                = (r.account_id, r.consumer_id, r.delivery_config_id, r.delivery_config_version,
                   r.reporting_obligation_id, r.reporting_materialization_id, r.reporting_revision_id)
              AND m.record_kind = 'materialization'
              AND m.payload->>'status' IN ('available', 'delivered');
            observed_digest := reporting_wire_digest(r.payload->'observed_canonical_content_digest');
            expected_digest := reporting_wire_digest(expected_digest);
            IF outcome IS NULL
               OR r.payload->>'verification_profile'
                    IS DISTINCT FROM outcome#>>'{verification,verification_profile}'
               OR (r.payload->>'observed_row_count')::bigint IS DISTINCT FROM expected_rows
               OR reporting_sorted_totals(r.payload->'observed_control_totals')
                    IS DISTINCT FROM reporting_sorted_totals(outcome#>'{verification,control_totals}')
               OR (observed_digest IS NOT NULL AND observed_digest IS DISTINCT FROM expected_digest)
               OR (nullif(r.payload->>'observed_manifest_sha256', '') IS NOT NULL
                   AND r.payload->>'observed_manifest_sha256'
                       IS DISTINCT FROM outcome#>>'{resource,manifest_sha256}')
               OR (nullif(r.payload->>'observed_native_version_ref', '') IS NOT NULL
                   AND r.payload->>'observed_native_version_ref'
                       IS DISTINCT FROM outcome#>>'{resource,native_version_ref}')
               OR (r.payload->>'verification_profile' = 'canonical_digest'
                   AND (observed_digest IS NULL OR observed_digest IS DISTINCT FROM expected_digest))
               OR (r.payload->>'verification_profile' = 'manifest_checksums'
                   AND r.payload->>'observed_manifest_sha256'
                       IS DISTINCT FROM outcome#>>'{resource,manifest_sha256}')
               OR (r.payload->>'verification_profile' = 'native_commit'
                   AND r.payload->>'observed_native_version_ref'
                       IS DISTINCT FROM outcome#>>'{resource,native_version_ref}')
               OR (r.payload->>'observed_at')::timestamptz < (outcome->>'completed_at')::timestamptz
               OR (r.payload->>'observed_at')::timestamptz
                    >= (outcome#>>'{resource,expires_at}')::timestamptz THEN
                RAISE EXCEPTION 'reporting receipt acceptance is inconsistent' USING ERRCODE = '23514';
            END IF;
            RETURN;
        END IF;
        SELECT b.payload INTO binding FROM reporting_reconciliation_records b
        WHERE (b.account_id, b.consumer_id, b.delivery_config_id, b.delivery_config_version)
            = (r.account_id, r.consumer_id, r.delivery_config_id, r.delivery_config_version)
          AND b.record_kind = 'destination_binding';
        SELECT d.payload INTO delivery FROM reporting_reconciliation_records d
        WHERE (d.account_id, d.consumer_id, d.delivery_config_id, d.delivery_config_version,
               d.reporting_obligation_id)
            = (r.account_id, r.consumer_id, r.delivery_config_id, r.delivery_config_version,
               r.reporting_obligation_id)
          AND d.record_kind = 'obligation_delivery';
        observed_digest := reporting_wire_digest(r.payload#>'{verification,canonical_content_digest}');
        expected_digest := reporting_wire_digest(expected_digest);
        IF binding IS NULL OR delivery IS NULL
           OR (r.payload#>>'{verification,row_count}')::bigint IS DISTINCT FROM expected_rows
           OR reporting_sorted_totals(r.payload#>'{verification,control_totals}')
                IS DISTINCT FROM reporting_sorted_totals(expected_totals)
           OR (observed_digest IS NOT NULL AND observed_digest IS DISTINCT FROM expected_digest)
           OR (r.payload#>>'{verification,verification_profile}' = 'canonical_digest'
               AND (observed_digest IS NULL OR observed_digest IS DISTINCT FROM expected_digest))
           OR r.payload#>>'{verification,verified_at}' IS DISTINCT FROM r.payload->>'completed_at'
           OR nullif(r.payload#>'{verification,verified_format}', 'null'::jsonb)
                IS DISTINCT FROM nullif(binding->'format', 'null'::jsonb)
           OR r.payload#>'{resource,reader_compatibility}'
                IS DISTINCT FROM coalesce(binding->'reader_compatibility', '[]'::jsonb)
           OR r.payload#>>'{resource,kind}' IS DISTINCT FROM (CASE binding->>'method'
                WHEN 'file_transfer' THEN 'manifest' WHEN 'dataset_share' THEN 'dataset'
                ELSE 'warehouse_relation' END)
           OR (binding->>'method' = 'dataset_share'
               AND r.payload#>>'{verification,verification_path}' <> 'representative_consumer')
           OR (binding->>'method' = 'warehouse_materialization'
               AND (r.payload#>>'{verification,verification_path}' <> 'destination'
                    OR r.payload->>'status' <> 'delivered'))
           OR (r.payload->>'status' = 'delivered'
               AND r.payload#>>'{verification,verification_path}' <> 'destination')
           OR (binding->>'method' = 'file_transfer' AND (
                jsonb_array_length(coalesce(r.payload#>'{resource,object_refs}', '[]'::jsonb)) = 0
                OR jsonb_array_length(
                    coalesce(r.payload#>'{verification,physical_checksums}', '[]'::jsonb)) = 0))
           OR (r.payload#>>'{resource,expires_at}')::timestamptz < greatest(
                (delivery->>'resource_retained_until')::timestamptz,
                (r.payload->>'completed_at')::timestamptz
                    + ((binding->>'resource_retention_days') || ' days')::interval) THEN
            RAISE EXCEPTION 'reporting materialization evidence is inconsistent' USING ERRCODE = '23514';
        END IF;
        -- Every checksum names a retained object, whatever the method; file
        -- transfer must additionally cover all of them.
        IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(
                coalesce(r.payload#>'{verification,physical_checksums}', '[]'::jsonb)) AS c
            WHERE NOT coalesce(r.payload#>'{resource,object_refs}', '[]'::jsonb)
                @> jsonb_build_array(c->'object_ref'))
           OR (binding->>'method' = 'file_transfer' AND reporting_sorted_strings(
                (SELECT jsonb_agg(c->'object_ref')
                 FROM jsonb_array_elements(r.payload#>'{verification,physical_checksums}') AS c))
               IS DISTINCT FROM reporting_sorted_strings(r.payload#>'{resource,object_refs}')) THEN
            RAISE EXCEPTION 'reporting physical checksum binding is inconsistent' USING ERRCODE = '23514';
        END IF;
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_reconciliation_validate(r reporting_reconciliation_records)
    RETURNS VOID LANGUAGE plpgsql AS $function$
    DECLARE
        generation JSONB;
        identity TEXT;
        chain TEXT;
        expected_namespace TEXT;
        parent_kind TEXT;
    BEGIN
        -- The retained fingerprint must be a fact about the stored bytes, and the
        -- payload must be closed. Without both, ordinary direct SQL can persist a
        -- provider blob or credential under an attacker-chosen digest and leave the
        -- principal's whole feed unreadable. Never echo the offending value.
        IF r.content_sha256 IS DISTINCT FROM reporting_payload_sha256(r.payload)
           OR EXISTS (SELECT 1 FROM reporting_payload_keys(r.payload) AS field
                      WHERE field <> ALL (ARRAY[
            'account_id', 'adjusts_reporting_revision_id', 'algorithm', 'attempt',
            'canonical_content_digest', 'canonicalization_id', 'canonicalization_sha256',
            'canonicalization_uri', 'check_id', 'checked_at', 'completed_at',
            'consumer_commit_ref', 'consumer_id', 'control_totals', 'created_at', 'currency',
            'delivery_config_id', 'delivery_config_version', 'destination_ref', 'expires_at',
            'failure_code', 'feed_purpose', 'format', 'generation_key', 'immutability', 'kind',
            'location', 'manifest_sha256', 'method', 'name', 'native_observed_through',
            'native_version_ref', 'object_ref', 'object_refs', 'observed_adjustment_sha256',
            'observed_at', 'observed_canonical_content_digest', 'observed_control_totals',
            'observed_manifest_sha256', 'observed_native_version_ref', 'observed_row_count',
            'physical_checksums', 'reader_compatibility', 'received_at', 'reconciliation_mode',
            'rejection_codes', 'reporting_adjustment_id', 'reporting_materialization_id',
            'reporting_obligation_id', 'reporting_receipt_id', 'reporting_revision_id', 'resource',
            'resource_ref', 'resource_retained_until', 'resource_retention_days', 'row_count',
            'scope', 'state', 'status', 'success_status', 'supersedes_reporting_receipt_id',
            'transport', 'trusted_binding_ref', 'unit', 'value', 'value_type', 'verification',
            'verification_path', 'verification_profile', 'verified_at', 'verified_format'])) THEN
            RAISE EXCEPTION 'reporting payload is not closed retained evidence' USING ERRCODE = '23514';
        END IF;
        generation := jsonb_build_object('account_id', r.account_id,
            'delivery_config_id', r.delivery_config_id,
            'delivery_config_version', r.delivery_config_version);
        IF NOT EXISTS (SELECT 1 FROM reporting_configurations c
            WHERE (c.account_id, c.delivery_config_id, c.delivery_config_version)
                = (r.account_id, r.delivery_config_id, r.delivery_config_version))
           OR (r.reporting_obligation_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM reporting_obligations o
               WHERE (o.account_id, o.delivery_config_id, o.delivery_config_version, o.reporting_obligation_id)
                   = (r.account_id, r.delivery_config_id, r.delivery_config_version, r.reporting_obligation_id)))
           OR (r.reporting_revision_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM reporting_revisions v
               WHERE (v.account_id, v.reporting_obligation_id, v.reporting_revision_id)
                   = (r.account_id, r.reporting_obligation_id, r.reporting_revision_id)))
           OR (r.reporting_adjustment_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM reporting_adjustments a
               WHERE (a.account_id, a.adjusts_reporting_revision_id, a.reporting_adjustment_id)
                   = (r.account_id, r.reporting_revision_id, r.reporting_adjustment_id))) THEN
            RAISE EXCEPTION 'reporting exact publication graph is unavailable' USING ERRCODE = '23514';
        END IF;
        expected_namespace := CASE WHEN r.receipt_status IS NOT NULL THEN 'receipt' ELSE r.record_kind END;
        identity := CASE r.record_kind
            WHEN 'destination_binding' THEN reporting_identity_sha256(
                '{"account_id":' || to_json(r.account_id)::text || ',"delivery_config_id":' ||
                to_json(r.delivery_config_id)::text || ',"delivery_config_version":' ||
                r.delivery_config_version::text || '}')
            WHEN 'obligation_delivery' THEN r.reporting_obligation_id
            WHEN 'materialization_check' THEN r.payload->>'check_id'
            WHEN 'revision_receipt' THEN r.payload->>'reporting_receipt_id'
            WHEN 'adjustment_receipt' THEN r.payload->>'reporting_receipt_id'
            ELSE r.reporting_materialization_id END;
        chain := CASE r.record_kind
            WHEN 'revision_receipt' THEN reporting_identity_sha256(
                '["revision",' || to_json(r.reporting_obligation_id)::text || ',' ||
                to_json(r.reporting_revision_id)::text || ']')
            WHEN 'adjustment_receipt' THEN reporting_identity_sha256(
                '["adjustment",' || to_json(r.reporting_adjustment_id)::text || ']') END;
        IF r.payload->>'kind' IS DISTINCT FROM r.record_kind
           OR r.namespace IS DISTINCT FROM expected_namespace
           OR r.record_id IS DISTINCT FROM identity
           OR r.receipt_chain_key IS DISTINCT FROM chain
           OR r.change_id IS DISTINCT FROM reporting_identity_sha256(
                '[' || to_json(r.account_id)::text || ',' || to_json(r.consumer_id)::text || ',' ||
                to_json(expected_namespace)::text || ',' || to_json(identity)::text || ']')
           OR r.reporting_revision_id IS DISTINCT FROM (CASE WHEN r.record_kind = 'adjustment_receipt'
                THEN r.payload->>'adjusts_reporting_revision_id' ELSE r.payload->>'reporting_revision_id' END)
           OR r.reporting_materialization_id IS DISTINCT FROM r.payload->>'reporting_materialization_id'
           OR r.reporting_adjustment_id IS DISTINCT FROM r.payload->>'reporting_adjustment_id'
           OR coalesce(to_jsonb(r.attempt_number), 'null'::jsonb) IS DISTINCT FROM coalesce(r.payload->'attempt', 'null'::jsonb)
           OR r.receipt_status IS DISTINCT FROM (CASE WHEN r.record_kind IN ('revision_receipt', 'adjustment_receipt')
                THEN r.payload->>'status' END)
           OR r.supersedes_receipt_id IS DISTINCT FROM r.payload->>'supersedes_reporting_receipt_id' THEN
            RAISE EXCEPTION 'reporting payload identity is inconsistent' USING ERRCODE = '23514';
        END IF;
        IF r.record_kind = 'destination_binding' THEN
            IF r.payload->'generation_key' IS DISTINCT FROM generation
               OR r.payload->>'consumer_id' IS DISTINCT FROM r.consumer_id
               OR (r.payload->>'verification_profile' = 'manifest_checksums'
                   AND r.payload->>'method' <> 'file_transfer') THEN
                RAISE EXCEPTION 'reporting destination binding is inconsistent' USING ERRCODE = '23514';
            END IF;
            RETURN;
        END IF;
        IF r.payload->'scope' IS DISTINCT FROM jsonb_build_object('generation_key', generation,
                'consumer_id', r.consumer_id, 'reporting_obligation_id', r.reporting_obligation_id)
           OR NOT EXISTS (
                SELECT 1 FROM reporting_reconciliation_records b
                WHERE (b.account_id, b.consumer_id, b.delivery_config_id, b.delivery_config_version)
                    = (r.account_id, r.consumer_id, r.delivery_config_id, r.delivery_config_version)
                  AND b.record_kind = 'destination_binding') THEN
            RAISE EXCEPTION 'reporting frozen binding is unavailable' USING ERRCODE = '23514';
        END IF;
        IF r.record_kind = 'obligation_delivery' THEN RETURN; END IF;
        IF NOT EXISTS (
            SELECT 1 FROM reporting_reconciliation_records d
            WHERE (d.account_id, d.consumer_id, d.delivery_config_id, d.delivery_config_version, d.reporting_obligation_id)
                = (r.account_id, r.consumer_id, r.delivery_config_id, r.delivery_config_version, r.reporting_obligation_id)
              AND d.record_kind = 'obligation_delivery') THEN
            RAISE EXCEPTION 'reporting obligation delivery is unavailable' USING ERRCODE = '23514';
        END IF;
        parent_kind := CASE r.record_kind WHEN 'materialization' THEN 'materialization_attempt'
            WHEN 'materialization_check' THEN 'materialization' WHEN 'revision_receipt' THEN 'materialization' END;
        IF parent_kind IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM reporting_reconciliation_records p
            WHERE (p.account_id, p.consumer_id, p.delivery_config_id, p.delivery_config_version,
                   p.reporting_obligation_id, p.reporting_materialization_id)
                = (r.account_id, r.consumer_id, r.delivery_config_id, r.delivery_config_version,
                   r.reporting_obligation_id, r.reporting_materialization_id)
              AND p.record_kind = parent_kind
              AND (r.record_kind = 'materialization_check' OR p.reporting_revision_id = r.reporting_revision_id)
              AND (parent_kind = 'materialization_attempt' OR p.payload->>'status' IN ('available', 'delivered'))) THEN
            RAISE EXCEPTION 'reporting exact materialization graph is unavailable' USING ERRCODE = '23514';
        END IF;
        IF r.record_kind = 'materialization' AND r.payload->>'status' IN ('available', 'delivered') THEN
            IF EXISTS (SELECT 1 FROM reporting_reconciliation_records b
                WHERE (b.account_id, b.consumer_id, b.delivery_config_id, b.delivery_config_version)
                    = (r.account_id, r.consumer_id, r.delivery_config_id, r.delivery_config_version)
                  AND b.record_kind = 'destination_binding'
                  AND (r.payload->>'status' IS DISTINCT FROM b.payload->>'success_status'
                    OR r.payload#>>'{verification,verification_profile}' IS DISTINCT FROM b.payload->>'verification_profile')) THEN
                RAISE EXCEPTION 'reporting materialization binding is inconsistent' USING ERRCODE = '23514';
            END IF;
            IF r.payload#>>'{verification,verification_profile}' = 'native_commit' AND (
                r.payload#>>'{resource,immutability}' IS DISTINCT FROM 'native_version'
                OR r.payload#>>'{resource,native_version_ref}' IS NULL
                OR r.payload#>>'{verification,native_version_ref}' IS DISTINCT FROM r.payload#>>'{resource,native_version_ref}'
                OR r.payload#>>'{verification,native_observed_through}' IS DISTINCT FROM r.payload#>>'{verification,verification_path}'
                OR coalesce(r.payload#>>'{verification,verification_path}', '') NOT IN ('representative_consumer', 'destination')) THEN
                RAISE EXCEPTION 'reporting native commit is inconsistent' USING ERRCODE = '23514';
            END IF;
        END IF;
        IF r.record_kind = 'adjustment_receipt' AND NOT EXISTS (
            SELECT 1 FROM reporting_revisions v WHERE v.account_id = r.account_id
              AND v.reporting_obligation_id = r.reporting_obligation_id
              AND v.reporting_revision_id = r.reporting_revision_id
              AND v.finality = 'official' AND v.finalized_at IS NOT NULL) THEN
            RAISE EXCEPTION 'reporting adjustment requires its official revision' USING ERRCODE = '23514';
        END IF;
        IF r.supersedes_receipt_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM reporting_reconciliation_records p
            WHERE (p.account_id, p.consumer_id, p.record_kind, p.delivery_config_id,
                   p.delivery_config_version, p.reporting_obligation_id, p.reporting_revision_id, p.receipt_chain_key)
                = (r.account_id, r.consumer_id, r.record_kind, r.delivery_config_id,
                   r.delivery_config_version, r.reporting_obligation_id, r.reporting_revision_id, r.receipt_chain_key)
              AND p.record_id = r.supersedes_receipt_id AND p.receipt_status = 'rejected') THEN
            RAISE EXCEPTION 'reporting receipt predecessor is unavailable' USING ERRCODE = '23514';
        END IF;
        PERFORM reporting_reconciliation_evidence(r);
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_reconciliation_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        PERFORM reporting_reconciliation_validate(NEW);
        IF NEW.record_kind = 'materialization_attempt' AND NEW.attempt_number <> (
            SELECT count(*) + 1 FROM reporting_reconciliation_records p
            WHERE (p.account_id, p.consumer_id, p.reporting_obligation_id, p.reporting_revision_id)
                = (NEW.account_id, NEW.consumer_id, NEW.reporting_obligation_id, NEW.reporting_revision_id)
              AND p.record_kind = 'materialization_attempt') THEN
            RAISE EXCEPTION 'reporting attempt ordinal is invalid' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_receipt_head_exact()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM reporting_reconciliation_records r
            WHERE (r.account_id, r.consumer_id, r.namespace, r.record_id, r.receipt_chain_key, r.receipt_status)
                = (NEW.account_id, NEW.consumer_id, NEW.namespace, NEW.receipt_id, NEW.chain_key, NEW.receipt_status)
              AND r.supersedes_receipt_id IS NOT DISTINCT FROM NEW.supersedes_receipt_id
              AND NOT EXISTS (SELECT 1 FROM reporting_reconciliation_records s
                  WHERE s.account_id = r.account_id AND s.consumer_id = r.consumer_id
                    AND s.supersedes_receipt_id = r.record_id)) THEN
            RAISE EXCEPTION 'reporting receipt head is inconsistent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_receipt_advance()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        IF NEW.receipt_status IS NULL THEN RETURN NEW; END IF;
        IF NEW.supersedes_receipt_id IS NULL THEN
            INSERT INTO reporting_receipt_heads(account_id, consumer_id, chain_key, receipt_id, receipt_status)
                VALUES (NEW.account_id, NEW.consumer_id, NEW.receipt_chain_key, NEW.record_id, NEW.receipt_status);
        ELSE
            UPDATE reporting_receipt_heads SET receipt_id = NEW.record_id, receipt_status = NEW.receipt_status,
                supersedes_receipt_id = NEW.supersedes_receipt_id
            WHERE account_id = NEW.account_id AND consumer_id = NEW.consumer_id
                AND chain_key = NEW.receipt_chain_key AND receipt_id = NEW.supersedes_receipt_id
                AND receipt_status = 'rejected';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'reporting receipt replacement is unavailable' USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_reconciliation_reference_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    DECLARE
        previous JSONB := to_jsonb(OLD);
        proposed JSONB := to_jsonb(NEW);
        referenced BOOLEAN;
        -- Period-close leasing plus the rc.3 lifecycle state that evolves over
        -- one immutable generation: reporting-delivery-config-state.json moves
        -- the same generation through ready -> inactive, requiring
        -- deactivated_at, and its operational recovery/retention windows are
        -- likewise state rather than published content. Reconciliation records
        -- bind the generation key and its content, none of which these carry.
        mutable TEXT[] := ARRAY['lease_worker_id', 'lease_expires_at', 'activated_at',
                                'deactivated_at', 'automated_recovery_seconds',
                                'status_retention_days'];
    BEGIN
        IF proposed = previous
           OR (TG_TABLE_NAME = 'reporting_configurations' AND
               proposed - mutable = previous - mutable)
           OR (TG_TABLE_NAME = 'reporting_revisions' AND proposed - 'readable' = previous - 'readable') THEN
            RETURN NEW;
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || OLD.account_id));
        IF TG_TABLE_NAME = 'reporting_configurations' THEN
            previous := previous - mutable;
            proposed := proposed - mutable;
            SELECT EXISTS (SELECT 1 FROM reporting_reconciliation_records r
                WHERE r.account_id = OLD.account_id AND r.delivery_config_id = OLD.delivery_config_id
                    AND r.delivery_config_version = OLD.delivery_config_version) INTO referenced;
        ELSIF TG_TABLE_NAME = 'reporting_obligations' THEN
            SELECT EXISTS (SELECT 1 FROM reporting_reconciliation_records r
                WHERE r.account_id = OLD.account_id AND r.reporting_obligation_id = OLD.reporting_obligation_id) INTO referenced;
        ELSIF TG_TABLE_NAME = 'reporting_revisions' THEN
            previous := previous - 'readable';
            proposed := proposed - 'readable';
            SELECT EXISTS (SELECT 1 FROM reporting_reconciliation_records r
                WHERE r.account_id = OLD.account_id AND r.reporting_revision_id = OLD.reporting_revision_id) INTO referenced;
        ELSE
            SELECT EXISTS (SELECT 1 FROM reporting_reconciliation_records r
                WHERE r.account_id = OLD.account_id AND r.reporting_adjustment_id = OLD.reporting_adjustment_id) INTO referenced;
        END IF;
        IF referenced AND proposed IS DISTINCT FROM previous THEN
            RAISE EXCEPTION 'reporting referenced publication is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    FOREACH graph_table IN ARRAY ARRAY['reporting_configurations', 'reporting_obligations',
                                       'reporting_revisions', 'reporting_adjustments'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = graph_table::regclass
                       AND tgname = 'reporting_reconciliation_reference_immutable' AND NOT tgisinternal) THEN
            EXECUTE format('CREATE TRIGGER reporting_reconciliation_reference_immutable BEFORE UPDATE ON %I '
                'FOR EACH ROW EXECUTE FUNCTION reporting_reconciliation_reference_immutable()', graph_table);
        END IF;
    END LOOP;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_reconciliation_records'::regclass
                   AND tgname = 'reporting_reconciliation_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_reconciliation_guard BEFORE INSERT ON reporting_reconciliation_records
            FOR EACH ROW EXECUTE FUNCTION reporting_reconciliation_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_reconciliation_records'::regclass
                   AND tgname = 'reporting_receipt_advance' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_receipt_advance AFTER INSERT ON reporting_reconciliation_records
            FOR EACH ROW EXECUTE FUNCTION reporting_receipt_advance();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_receipt_heads'::regclass
                   AND tgname = 'reporting_receipt_head_exact' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_receipt_head_exact BEFORE INSERT OR UPDATE ON reporting_receipt_heads
            FOR EACH ROW EXECUTE FUNCTION reporting_receipt_head_exact();
    END IF;

    -- Revalidate old extension rows too; installing a trigger alone would bless
    -- an already-corrupt graph. A failed upgrade leaves all prior data intact.
    FOR retained_record IN SELECT r FROM reporting_reconciliation_records r LOOP
        PERFORM reporting_reconciliation_validate(retained_record.r);
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM reporting_reconciliation_records r
        LEFT JOIN reporting_receipt_heads h ON (h.account_id, h.consumer_id, h.chain_key, h.receipt_id, h.receipt_status)
            = (r.account_id, r.consumer_id, r.receipt_chain_key, r.record_id, r.receipt_status)
        WHERE r.receipt_status IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM reporting_reconciliation_records s
              WHERE s.account_id = r.account_id AND s.consumer_id = r.consumer_id AND s.supersedes_receipt_id = r.record_id)
          AND (h.receipt_id IS NULL OR h.supersedes_receipt_id IS DISTINCT FROM r.supersedes_receipt_id)
    ) OR EXISTS (
        SELECT 1 FROM (SELECT attempt_number, row_number() OVER (
            PARTITION BY account_id, consumer_id, reporting_obligation_id, reporting_revision_id
            ORDER BY attempt_number) AS ordinal FROM reporting_reconciliation_records
            WHERE record_kind = 'materialization_attempt') attempts WHERE attempt_number <> ordinal
    ) THEN
        RAISE EXCEPTION 'reporting retained graph is inconsistent' USING ERRCODE = '23514';
    END IF;

    -- The optional feed has a dense head for each authenticated principal. It
    -- never allocates Core ledger sequences or widens Core's record vocabulary.
    feed_installed := to_regclass('reporting_reconciliation_changes') IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_reconciliation_feed_identity
        ON reporting_reconciliation_records(account_id, consumer_id, namespace, record_id,
                                             record_kind, change_id, content_sha256);
    CREATE TABLE IF NOT EXISTS reporting_reconciliation_changes (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        seq BIGINT NOT NULL CHECK (seq > 0),
        namespace TEXT COLLATE "C" NOT NULL,
        record_id TEXT COLLATE "C" NOT NULL,
        record_kind TEXT NOT NULL,
        change_id TEXT COLLATE "C" NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id, consumer_id, seq),
        UNIQUE (account_id, consumer_id, namespace, record_id),
        UNIQUE (account_id, consumer_id, namespace, record_id, record_kind, change_id, content_sha256),
        FOREIGN KEY (account_id, consumer_id, namespace, record_id, record_kind, change_id, content_sha256)
            REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id,
                                                         record_kind, change_id, content_sha256)
    );
    CREATE TABLE IF NOT EXISTS reporting_reconciliation_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        max_sequence BIGINT NOT NULL CHECK (max_sequence > 0),
        PRIMARY KEY (account_id, consumer_id),
        FOREIGN KEY (account_id, consumer_id, max_sequence)
            REFERENCES reporting_reconciliation_changes(account_id, consumer_id, seq)
            DEFERRABLE INITIALLY DEFERRED
    );

    IF NOT feed_installed THEN
        -- Project the initial storage slice's complete change history into the
        -- independent feed. Preserve every original record, head, legacy change,
        -- hash and timestamp. Missing evidence is never repaired or inferred.
        IF EXISTS (
            SELECT 1 FROM reporting_reconciliation_records r
            LEFT JOIN reporting_ledger_changes c ON c.account_id = r.account_id
                AND c.record_kind = r.record_kind AND c.record_id = r.change_id
            WHERE c.seq IS NULL
        ) THEN
            RAISE EXCEPTION 'reporting legacy feed is incomplete' USING ERRCODE = '23514';
        END IF;
        INSERT INTO reporting_reconciliation_changes
            (account_id, consumer_id, seq, namespace, record_id, record_kind,
             change_id, content_sha256, committed_at)
        SELECT r.account_id, r.consumer_id,
            row_number() OVER (PARTITION BY r.account_id, r.consumer_id ORDER BY c.seq),
            r.namespace, r.record_id, r.record_kind, r.change_id, r.content_sha256, c.committed_at
        FROM reporting_reconciliation_records r
        JOIN reporting_ledger_changes c ON c.account_id = r.account_id
            AND c.record_kind = r.record_kind AND c.record_id = r.change_id;
        INSERT INTO reporting_reconciliation_heads (account_id, consumer_id, max_sequence)
            SELECT account_id, consumer_id, max(seq) FROM reporting_reconciliation_changes
            GROUP BY account_id, consumer_id;
    END IF;

    -- Earlier audit builds tied records to Core's feed. Replace only that
    -- obsolete FK; the legacy change rows themselves remain byte-for-byte.
    ALTER TABLE reporting_reconciliation_records
        DROP CONSTRAINT IF EXISTS reporting_record_transactional_change;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'reporting_reconciliation_records'::regclass
                     AND conname = 'reporting_record_transactional_feed') THEN
        ALTER TABLE reporting_reconciliation_records
            ADD CONSTRAINT reporting_record_transactional_feed
            FOREIGN KEY (account_id, consumer_id, namespace, record_id, record_kind, change_id, content_sha256)
            REFERENCES reporting_reconciliation_changes(account_id, consumer_id, namespace, record_id,
                                                         record_kind, change_id, content_sha256)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;

    CREATE OR REPLACE FUNCTION reporting_reconciliation_head_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'reporting feed head is append-only' USING ERRCODE = '23514';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        IF (TG_OP = 'INSERT' AND NEW.max_sequence <> 1)
           OR (TG_OP = 'UPDATE' AND (
                NEW.account_id <> OLD.account_id OR NEW.consumer_id <> OLD.consumer_id
                OR NEW.max_sequence <> OLD.max_sequence + 1)) THEN
            RAISE EXCEPTION 'reporting feed head transition is invalid' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    CREATE OR REPLACE FUNCTION reporting_reconciliation_change_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        IF NOT EXISTS (SELECT 1 FROM reporting_reconciliation_heads h
            WHERE h.account_id = NEW.account_id AND h.consumer_id = NEW.consumer_id
              AND h.max_sequence = NEW.seq)
           OR (NEW.seq > 1 AND NOT EXISTS (
                SELECT 1 FROM reporting_reconciliation_changes c
                WHERE c.account_id = NEW.account_id AND c.consumer_id = NEW.consumer_id
                  AND c.seq = NEW.seq - 1)) THEN
            RAISE EXCEPTION 'reporting feed sequence is inconsistent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_reconciliation_heads'::regclass
                   AND tgname = 'reporting_reconciliation_head_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_reconciliation_head_guard
            BEFORE INSERT OR UPDATE OR DELETE ON reporting_reconciliation_heads
            FOR EACH ROW EXECUTE FUNCTION reporting_reconciliation_head_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_reconciliation_changes'::regclass
                   AND tgname = 'reporting_reconciliation_change_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_reconciliation_change_guard BEFORE INSERT ON reporting_reconciliation_changes
            FOR EACH ROW EXECUTE FUNCTION reporting_reconciliation_change_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_reconciliation_changes'::regclass
                   AND tgname = 'reporting_reconciliation_change_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_reconciliation_change_immutable
            BEFORE UPDATE OR DELETE ON reporting_reconciliation_changes
            FOR EACH ROW EXECUTE FUNCTION reporting_reconciliation_immutable();
    END IF;
    IF EXISTS (
        SELECT 1 FROM (SELECT account_id, consumer_id, count(*) AS count, max(seq) AS maximum
                       FROM reporting_reconciliation_changes GROUP BY account_id, consumer_id) c
        FULL JOIN reporting_reconciliation_heads h USING (account_id, consumer_id)
        WHERE c.count IS NULL OR h.max_sequence IS NULL OR c.count <> c.maximum
           OR h.max_sequence <> c.maximum
    ) THEN
        RAISE EXCEPTION 'reporting retained feed is inconsistent' USING ERRCODE = '23514';
    END IF;
END
$reconciliation$;
