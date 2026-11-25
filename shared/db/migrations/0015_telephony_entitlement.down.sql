-- Rollback 0015: telephony stops being a gated capability (everyone may
-- register trunks and numbers again, as before the signup reopening).

ALTER TABLE entitlements DROP COLUMN IF EXISTS telephony;
ALTER TABLE plans DROP COLUMN IF EXISTS telephony_enabled;
