-- 0007: follow-up auto-dialer attempt tracking.
--
-- The follow-up engine decided and scheduled, but nothing ever placed the
-- call: sweep_overdue() flipped every due row to 'missed' an hour later, so
-- 100% of follow-ups ended un-actioned (0 completed of 155 as of 2026-07-22).
-- The executor needs per-row attempt state to enforce a retry cap and a
-- minimum gap between tries; without it a restart loop would redial the same
-- customer indefinitely.

ALTER TABLE followups ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE followups ADD COLUMN last_attempt_at TIMESTAMPTZ;
-- Why the most recent attempt did not result in a placed call (identity not
-- configured, outside window, capped, DND…). Surfaced in the UI so a stalled
-- follow-up explains itself instead of silently ageing into 'missed'.
ALTER TABLE followups ADD COLUMN last_attempt_error TEXT;
-- Set when a guardrail permanently stops auto-dialling this row (retry cap
-- reached, DND). Distinct from a transient skip, which leaves it eligible.
ALTER TABLE followups ADD COLUMN autodial_blocked_at TIMESTAMPTZ;

-- The executor's hot query: due, not blocked, oldest first.
CREATE INDEX idx_followups_autodial
    ON followups (owner, scheduled_at)
    WHERE deleted_at IS NULL
      AND autodial_blocked_at IS NULL
      AND status IN ('pending', 'scheduled', 'missed');
