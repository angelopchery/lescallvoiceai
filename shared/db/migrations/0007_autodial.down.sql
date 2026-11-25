DROP INDEX IF EXISTS idx_followups_autodial;
ALTER TABLE followups DROP COLUMN IF EXISTS autodial_blocked_at;
ALTER TABLE followups DROP COLUMN IF EXISTS last_attempt_error;
ALTER TABLE followups DROP COLUMN IF EXISTS last_attempt_at;
ALTER TABLE followups DROP COLUMN IF EXISTS attempts;
