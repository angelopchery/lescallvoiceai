-- 0008: follow-up execution modes — auto-dialling becomes OPT-IN.
--
-- 0007's executor dialled EVERY decision='yes' follow-up: there was no
-- per-follow-up or per-agent choice, which inverted the product intent
-- ("manual unless switched to automatic"). It placed its first 12 real
-- calls the moment the 10:00 IST window opened on 2026-07-23. Default
-- 'manual' restores opt-in; rows already dialled keep their attempt
-- history and simply stop being candidates until someone flips them.
ALTER TABLE followups ADD COLUMN mode TEXT NOT NULL DEFAULT 'manual'
    CHECK (mode IN ('manual', 'auto'));
-- NULL = platform default (config.AUTODIAL_MAX_ATTEMPTS). Per-row override
-- so one important follow-up can retry harder without a global knob.
ALTER TABLE followups ADD COLUMN max_attempts INTEGER;
-- Explicit next-try time (linear backoff written by record_attempt). NULL =
-- legacy implicit gap (last_attempt_at + AUTODIAL_RETRY_GAP_MIN). UI-visible
-- so "why isn't it calling yet" answers itself.
ALTER TABLE followups ADD COLUMN next_attempt_at TIMESTAMPTZ;
-- The follow-up call that fulfilled this row, once it exists/analyzes.
ALTER TABLE followups ADD COLUMN executed_call_id BIGINT
    REFERENCES calls(id) ON DELETE SET NULL;
-- answered | no_answer | busy | declined | failed — from the FS originate
-- result, upgraded to 'answered' by the analysis-time completion hook.
ALTER TABLE followups ADD COLUMN execution_outcome TEXT;

-- Link each auto-dial to its follow-up so the outcome loop joins on data,
-- not on time-window heuristics.
ALTER TABLE call_links ADD COLUMN followup_id BIGINT;

-- Same hot query as 0007, now scoped to opted-in rows.
DROP INDEX IF EXISTS idx_followups_autodial;
CREATE INDEX idx_followups_autodial
    ON followups (owner, scheduled_at)
    WHERE deleted_at IS NULL
      AND autodial_blocked_at IS NULL
      AND mode = 'auto'
      AND status IN ('pending', 'scheduled', 'missed');
