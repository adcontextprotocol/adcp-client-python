-- Optional fixed-profile service context. Existing B2 documents stay valid.
DO $service_context$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    CREATE OR REPLACE FUNCTION reporting_production_service_context_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
    DECLARE
        facts JSONB := NEW.source_binding->'service_context';
        zone TEXT;
        configuration_hash TEXT;
    BEGIN
        IF facts IS NULL AND NOT NEW.source_binding ? 'service_context_sha256' THEN
            RETURN NEW;
        END IF;
        SELECT account_timezone,reporting_payload_sha256(jsonb_build_object(
            'account_id',account_id,'report_definition_id',report_definition_id,
            'reporting_profile',reporting_profile,'feed_purpose',feed_purpose,
            'required_finality',required_finality,'account_timezone',account_timezone,
            'authoritative_party',authoritative_party,'media_buy_ids',media_buy_ids,
            'definition',definition,'schedule',schedule))
        INTO zone,configuration_hash FROM reporting_configurations
        WHERE account_id=NEW.account_id AND delivery_config_id=NEW.delivery_config_id
            AND delivery_config_version=NEW.delivery_config_version;
        IF jsonb_typeof(facts) IS DISTINCT FROM 'object'
            OR NEW.source_binding->>'service_context_sha256'
                IS DISTINCT FROM reporting_payload_sha256(facts)
            OR facts->'version' IS DISTINCT FROM '1'::jsonb
            OR facts->>'account_id' IS DISTINCT FROM NEW.account_id
            OR facts->>'account_timezone' IS DISTINCT FROM zone
            OR NEW.source_binding->>'configuration_sha256' IS DISTINCT FROM configuration_hash
            OR NEW.source_binding->>'account_id' IS DISTINCT FROM NEW.account_id
            OR NEW.source_binding->>'delivery_config_id' IS DISTINCT FROM NEW.delivery_config_id
            OR NEW.source_binding->'delivery_config_version'
                IS DISTINCT FROM to_jsonb(NEW.delivery_config_version)
            OR facts->>'capabilities_sha256'
                IS DISTINCT FROM NEW.source_binding->>'capabilities_sha256'
            OR facts->>'currency' IS NULL OR facts->>'currency' !~ '^[A-Z]{3}$'
            OR facts->>'adapter' IS NULL OR facts->>'adapter' !~ '^[A-Za-z0-9_.:-]{1,128}$'
            OR NOT facts ?& ARRAY['version','adapter','account_id','account_timezone',
                'capabilities_sha256','currency','offering_id','source_offering_id','snapshot_offering_id',
                'official_offering_id','publication_namespace','requested_metrics',
                'requested_dimensions','source_scope','slice_timeout_microseconds']
            OR (SELECT count(*) FROM jsonb_object_keys(facts)) <> 15
        THEN
            RAISE EXCEPTION 'reporting service context is inconsistent';
        END IF;
        RETURN NEW;
    END
    $guard$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_production_generations'::regclass
            AND tgname='reporting_production_service_context_consistent') THEN
        CREATE TRIGGER reporting_production_service_context_consistent BEFORE INSERT
        ON reporting_production_generations FOR EACH ROW
        EXECUTE FUNCTION reporting_production_service_context_guard();
    END IF;
END
$service_context$;
