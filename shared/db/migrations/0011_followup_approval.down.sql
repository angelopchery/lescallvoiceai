-- Reverting drops the human-approval record. The auto-dialer immediately
-- treats every mode='auto' row as diallable again — do not run this on a live
-- tenant without setting INTEL_AUTODIAL_ENABLED=false first.
DROP INDEX IF EXISTS idx_followups_review;
DROP INDEX IF EXISTS idx_followups_autodial;
CREATE INDEX idx_followups_autodial
    ON followups (owner, scheduled_at)
    WHERE deleted_at IS NULL
      AND autodial_blocked_at IS NULL
      AND mode = 'auto'
      AND status IN ('pending', 'scheduled', 'missed');
ALTER TABLE followups DROP COLUMN IF EXISTS declined_by;
ALTER TABLE followups DROP COLUMN IF EXISTS declined_at;
ALTER TABLE followups DROP COLUMN IF EXISTS approval_expires_at;
ALTER TABLE followups DROP COLUMN IF EXISTS approved_by;
ALTER TABLE followups DROP COLUMN IF EXISTS approved_at;
ALTER TABLE followups DROP COLUMN IF EXISTS approval_state;
