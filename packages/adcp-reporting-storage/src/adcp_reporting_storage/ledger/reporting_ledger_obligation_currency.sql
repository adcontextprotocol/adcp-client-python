-- #1171: retain the currency frozen before source work. Apply after the #1169
-- account-generations migration. This DO statement is atomic in autocommit too.
-- Stop older writers first: they cannot freeze currency for new obligations.
--
-- There is deliberately NO backfill and NO USD default. Neither beta.15 nor
-- #1169 retained sufficient evidence to prove arbitrary historical currency.
-- NULL means unknown: history remains readable, but new source work, revisions
-- and adjustments must fail closed. See docs/reporting-ledger-migration.md.

DO $obligation_currency$
DECLARE
    currency_type OID;
    currency_default TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    LOCK TABLE reporting_obligations IN ACCESS EXCLUSIVE MODE;

    ALTER TABLE reporting_obligations ADD COLUMN IF NOT EXISTS currency TEXT COLLATE "C";

    SELECT a.atttypid, pg_get_expr(d.adbin, d.adrelid)
    INTO currency_type, currency_default
    FROM pg_attribute a
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attrelid = 'reporting_obligations'::regclass AND a.attname = 'currency';
    IF currency_type <> 'text'::regtype OR currency_default IS NOT NULL THEN
        RAISE EXCEPTION 'Unexpected reporting_obligations.currency type or default'
            USING HINT = 'Inspect the adopter schema; currency must be text with no inferred default.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reporting_obligations'::regclass
          AND conname = 'reporting_obligations_currency_code'
    ) THEN
        ALTER TABLE reporting_obligations ADD CONSTRAINT reporting_obligations_currency_code
            CHECK (currency IS NULL OR (octet_length(currency) = 3 AND currency COLLATE "C" ~ '^[A-Z]{3}$'));
    END IF;

    -- Protect the new immutable fact even from a direct UPDATE/upsert. A
    -- historical repair needs its own reviewed, evidence-backed migration.
    CREATE OR REPLACE FUNCTION reporting_obligation_currency_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        IF NEW.currency IS DISTINCT FROM OLD.currency THEN
            RAISE EXCEPTION 'reporting obligation currency is immutable'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END
    $function$;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'reporting_obligations'::regclass
          AND tgname = 'reporting_obligation_currency_immutable'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER reporting_obligation_currency_immutable
            BEFORE UPDATE OF currency ON reporting_obligations
            FOR EACH ROW EXECUTE FUNCTION reporting_obligation_currency_immutable();
    END IF;
END
$obligation_currency$;
