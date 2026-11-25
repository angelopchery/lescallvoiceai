-- Rollback of 0025. Admin-granted credits are DELETED, not just un-columned:
-- leaving them behind as source-less rows would make every one of them look
-- like a paid top-up to `get_entitlements()`, which would keep handing out the
-- minutes with no record of where they came from.
DELETE FROM minute_topups WHERE source = 'admin_credit';

DROP INDEX IF EXISTS idx_topups_live;

ALTER TABLE minute_topups DROP COLUMN IF EXISTS settled_through;
ALTER TABLE minute_topups DROP COLUMN IF EXISTS minutes_consumed;
ALTER TABLE minute_topups DROP COLUMN IF EXISTS note;
ALTER TABLE minute_topups DROP COLUMN IF EXISTS granted_by;
ALTER TABLE minute_topups DROP COLUMN IF EXISTS source;
