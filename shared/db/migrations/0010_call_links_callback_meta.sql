-- 0010: structured callback metadata for scheduled follow-up calls.
--
-- Platform rule (2026-07-27): a follow-up call the auto-dialer places must
-- OPEN by referencing the earlier conversation ("we spoke earlier today
-- about X and you'd asked me to call you back"). context_note already feeds
-- the WHY-YOU-ARE-CALLING-BACK prompt band but is free text; the opening
-- line needs structured fields. JSON: {reason, topic, requested_when,
-- source_call_at}. NULL = not a scheduled follow-up (normal cold outbound).
ALTER TABLE call_links ADD COLUMN callback_meta TEXT;
