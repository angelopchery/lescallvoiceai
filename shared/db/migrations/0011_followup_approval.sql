-- 0011: NO follow-up call is placed without an explicit human approval.
--
-- Incident 2026-07-29: the auto-dialer called a real customer (+91 87678
-- 65050, follow-up 216 -> call 304) two days after a 44-second test call.
-- Nobody had promised anything on that call; the pipeline labelled it
-- promised_by='agent_promised', confidence 0.9, and dialled. A review of the
-- 20 armed rows found the same defect throughout: calls that failed on OUR
-- side ("call failed due to technical audio issues") were recorded as
-- agent_promised with confidence 1.0.
--
-- This is the SECOND time this happened. 0008 made auto-dialling opt-in after
-- 0007's executor placed 12 unwanted calls on 2026-07-23; commit b693a8c then
-- flipped the per-agent default back ON and the hole reopened. The lesson is
-- that `mode` is a PREFERENCE and a code default can synthesise it. Approval
-- is different in kind: it is a fact about a human action, it names that
-- human, and no config default can manufacture it. That is why it is a
-- separate column and not another mode value.
--
-- Existing rows: the DEFAULT applies to every row already in the table, so
-- all ~180 live follow-ups (including the 20 currently mode='auto') land in
-- 'pending' and become un-diallable until a person approves them. That is the
-- intended blast radius of this migration.
ALTER TABLE followups ADD COLUMN approval_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (approval_state IN ('pending', 'approved', 'declined'));

-- Who authorised the call, and when. Stamped only by the approve endpoint,
-- never by the pipeline. These two columns are the audit answer to "who told
-- us we could ring this person".
ALTER TABLE followups ADD COLUMN approved_at TIMESTAMPTZ;
ALTER TABLE followups ADD COLUMN approved_by TEXT;

-- An approval is consent to call around a particular time, not a standing
-- licence. Past this moment the row falls back to 'pending' and needs a fresh
-- human look. Defence in depth behind the existing too_old (72h) guard.
ALTER TABLE followups ADD COLUMN approval_expires_at TIMESTAMPTZ;

-- An explicit "do not call this one". Distinct from cancelling the follow-up:
-- the row stays visible and manually callable, it just may never auto-dial.
ALTER TABLE followups ADD COLUMN declined_at TIMESTAMPTZ;
ALTER TABLE followups ADD COLUMN declined_by TEXT;

-- The auto-dialer's hot query, now scoped to approved rows. Kept in lockstep
-- with _DUE_SQL in shared/intelligence/autodial.py — if you add a predicate
-- there, add it here.
DROP INDEX IF EXISTS idx_followups_autodial;
CREATE INDEX idx_followups_autodial
    ON followups (owner, scheduled_at)
    WHERE deleted_at IS NULL
      AND autodial_blocked_at IS NULL
      AND mode = 'auto'
      AND approval_state = 'approved'
      AND status IN ('pending', 'scheduled', 'missed');

-- The review queue: what a human is being asked to authorise.
CREATE INDEX idx_followups_review
    ON followups (owner, scheduled_at)
    WHERE deleted_at IS NULL
      AND approval_state = 'pending'
      AND status IN ('pending', 'scheduled', 'missed');
