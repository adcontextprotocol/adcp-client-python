-- Issue-lifecycle events for the adopter's issue observer (adcp-client-python#1258).
--
-- A transactional outbox: every write that moves a reporting_issue_lifecycle
-- row (an occurrence opening, being acknowledged or waived, or being retired as
-- resolved) records one row here in the same transaction, and the escalation
-- sweep records 'escalated' when an open or acknowledged occurrence passes
-- opened_at + consumer_mismatch_escalation_seconds. A worker claims pending
-- rows under a lease, hands each to the observer and stamps delivered_at.
--
-- UNIQUE (issue_id, kind) is the exactly-once guarantee: the issue lifecycle is
-- forward-only, so an occurrence makes each transition at most once, and two
-- replicas recording the same transition collide here instead of producing two
-- events. event_id is derived from (issue_id, kind), so it is stable across
-- retries and redeliveries and is what an observer deduplicates on.
--
-- Additive only: one new table and its indexes. No existing table, key,
-- function or trigger changes, so processes of earlier releases, which neither
-- read nor write this table, keep running beside it. Those processes record no
-- events for the transitions they make.
CREATE TABLE IF NOT EXISTS reporting_issue_events (
    event_id         TEXT COLLATE "C" NOT NULL PRIMARY KEY,
    -- Recording order. Per account it is commit order (every write here holds
    -- the account's advisory lock), and the worker delivers one occurrence's
    -- events in this order.
    seq              BIGSERIAL        NOT NULL,
    account_id       TEXT COLLATE "C" NOT NULL,
    issue_key        TEXT COLLATE "C" NOT NULL,
    generation       INTEGER          NOT NULL,
    issue_id         TEXT COLLATE "C" NOT NULL,
    consumer_id      TEXT COLLATE "C",
    kind             TEXT COLLATE "C" NOT NULL CHECK
        (kind IN ('opened', 'escalated', 'acknowledged', 'waived', 'resolved')),
    -- When the transition took effect: opened_at, the escalation boundary, the
    -- operator's instant, or retired_at.
    occurred_at      TIMESTAMPTZ      NOT NULL,
    recorded_at      TIMESTAMPTZ      NOT NULL,
    -- The next delivery attempt; a failed attempt moves it forward.
    due_at           TIMESTAMPTZ      NOT NULL,
    attempts         INTEGER          NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token      TEXT COLLATE "C",
    lease_expires_at TIMESTAMPTZ,
    delivered_at     TIMESTAMPTZ,
    UNIQUE (issue_id, kind),
    CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
);

CREATE INDEX IF NOT EXISTS reporting_issue_events_pending
    ON reporting_issue_events (due_at, seq)
    WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS reporting_issue_events_issue_pending
    ON reporting_issue_events (issue_id, seq)
    WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS reporting_issue_events_delivered
    ON reporting_issue_events (delivered_at)
    WHERE delivered_at IS NOT NULL;
