-- AdCP decisioning task registry — durable HITL task state.
--
-- Run this once per deployment. Tracked by PostgresTaskRegistry;
-- see src/adcp/decisioning/pg/task_registry.py for the query shapes
-- the Python code executes.
--
-- COLLATE "C" on identifier columns avoids locale-dependent case
-- folding — on some locales "Task-A" and "task-a" compare equal,
-- which could collapse distinct task_ids or account_ids. "C" is the
-- byte-for-byte comparison we actually want.
--
-- Alternatively, call PostgresTaskRegistry.create_schema() from
-- application code — it runs the equivalent DDL idempotently on boot.

CREATE TABLE IF NOT EXISTS decisioning_tasks (
    task_id     TEXT             COLLATE "C" NOT NULL PRIMARY KEY,
    account_id  TEXT             COLLATE "C" NOT NULL,
    state       TEXT             NOT NULL DEFAULT 'submitted',
    task_type   TEXT             NOT NULL,
    progress    JSONB,
    result      JSONB,
    error       JSONB,
    -- Unix epoch seconds (float), matches TaskRecord.created_at/updated_at
    -- so Python round-trips the value without lossy TIMESTAMPTZ conversion.
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);

-- Supports the cross-tenant get() query: WHERE task_id = $1 AND account_id = $2.
-- Without this index, every tasks/get is a full-table scan on account_id.
CREATE INDEX IF NOT EXISTS decisioning_tasks_account_idx
    ON decisioning_tasks (account_id);
