-- 0015_telephony_entitlement.sql — connecting a real phone number is a paid
-- capability.
--
-- Signups reopen to any Google account (the funnel is: sign up free → build an
-- agent → buy a plan → connect a number → take real calls). That means the
-- BYON panel is now reachable by strangers, and it is the one surface that
-- puts SIP trunks and DIDs on our carrier edge. So Free can look, build and
-- test in the dashboard, but cannot register a trunk or attach a number.
--
-- Pilot deliberately DOES include it: "Test LesCall With Your Own Business"
-- is a paid trial whose entire point is real calls on the customer's own
-- number, and it is the first paid step in the funnel.

ALTER TABLE plans ADD COLUMN telephony_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entitlements ADD COLUMN telephony INTEGER NOT NULL DEFAULT 0;

UPDATE plans SET telephony_enabled = 1
 WHERE code IN ('pilot', 'basic', 'growth', 'enterprise');
UPDATE plans SET telephony_enabled = 0 WHERE code = 'free';

-- Mirror onto the resolved rows. resolve_entitlements() recomputes this from
-- the plan on every call, but existing rows must be correct immediately —
-- the gate reads `entitlements`, not `plans`.
UPDATE entitlements e
   SET telephony = p.telephony_enabled,
       updated_at = now(),
       updated_by = 'migration:0015'
  FROM plans p
 WHERE p.code = e.plan_code;

-- Staff overrides carry their own grant (0014) and must keep everything on.
UPDATE entitlements SET telephony = 1
 WHERE owner IN ('tech@dhronai.com', 'angelo@dhronai.com',
                 'alwynantonyben@dhronai.com');

-- Anyone who ALREADY registered a trunk or a number keeps the capability,
-- whatever plan they are on. Taking away a working phone number from an
-- existing account would break live calls to real customers — a downgrade
-- must never destroy work in progress (the same rule agents follow).
--
-- This grant MUST go through `overrides`, not the plain column. The column is
-- derived state: resolve_entitlements() recomputes `telephony` from the plan
-- on every call and would silently strip a bare UPDATE the next time anything
-- touched the row. That is exactly how 0013's staff seed was lost — see 0014.
-- `pick()` reads overrides first, so this survives.
UPDATE entitlements
   SET overrides = (
           COALESCE(NULLIF(overrides, '')::jsonb, '{}'::jsonb)
           || '{"telephony": 1}'::jsonb
       )::text,
       telephony = 1,
       updated_at = now(),
       updated_by = 'migration:0015-grandfather'
 WHERE owner IN (SELECT owner FROM trunks WHERE owner IS NOT NULL
                 UNION SELECT owner FROM phone_numbers WHERE owner IS NOT NULL);
