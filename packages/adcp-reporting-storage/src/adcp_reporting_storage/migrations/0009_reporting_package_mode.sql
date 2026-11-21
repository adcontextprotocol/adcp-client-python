-- adcp-migration: 0009 reporting_package_mode
-- adcp-upgrade: stopped-worker
-- NULL identifies an obligation whose package reporting mode was not frozen.
ALTER TABLE reporting_obligations
    ADD COLUMN IF NOT EXISTS package_reporting BOOLEAN;

DO $package_mode_shape$
DECLARE
    mode_type OID;
    mode_default TEXT;
    mode_not_null BOOLEAN;
BEGIN
    SELECT a.atttypid, pg_get_expr(d.adbin, d.adrelid), a.attnotnull
    INTO mode_type, mode_default, mode_not_null
    FROM pg_attribute a
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attrelid = 'reporting_obligations'::regclass
      AND a.attname = 'package_reporting';
    IF mode_type <> 'boolean'::regtype OR mode_default IS NOT NULL OR mode_not_null THEN
        RAISE EXCEPTION 'Unexpected reporting_obligations.package_reporting shape'
            USING HINT = 'Package mode must be nullable with no default so retained rows remain unknown.';
    END IF;
END
$package_mode_shape$;

CREATE OR REPLACE FUNCTION reporting_obligation_package_mode_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
BEGIN
    IF NEW.package_reporting IS DISTINCT FROM OLD.package_reporting THEN
        RAISE EXCEPTION 'reporting obligation package mode is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

DO $package_mode_trigger$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'reporting_obligations'::regclass
          AND tgname = 'reporting_obligation_package_mode_immutable'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER reporting_obligation_package_mode_immutable
            BEFORE UPDATE OF package_reporting ON reporting_obligations
            FOR EACH ROW EXECUTE FUNCTION reporting_obligation_package_mode_immutable();
    END IF;
END
$package_mode_trigger$;
