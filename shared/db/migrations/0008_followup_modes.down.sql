DROP INDEX IF EXISTS idx_followups_autodial;
CREATE INDEX idx_followups_autodial
    ON followups (owner, scheduled_at)
    WHERE deleted_at IS NULL
      AND autodial_blocked_at IS NULL
      AND status IN ('pending', 'scheduled', 'missed');
ALTER TABLE call_links DROP COLUMN IF EXISTS followup_id;
ALTER TABLE followups DROP COLUMN IF EXISTS execution_outcome;
ALTER TABLE followups DROP COLUMN IF EXISTS executed_call_id;
ALTER TABLE followups DROP COLUMN IF EXISTS next_attempt_at;
ALTER TABLE followups DROP COLUMN IF EXISTS max_attempts;
ALTER TABLE followups DROP COLUMN IF EXISTS mode;
