-- 0022: Pilot gets the integration API too (owner decision, 2026-08-19).
--
-- 0021 excluded it on the reasoning that a 7-day trial ending with a customer's
-- CRM silently losing its feed is a bad first experience. Owner's call is the
-- opposite, and it is the stronger argument for a TRIAL specifically: a
-- prospect evaluating LesCall against an incumbent needs to see the data land
-- in their own CRM, not be told it would.
--
-- The expiry behaviour is unchanged and worth knowing: when the Pilot window
-- elapses the entitlement resolves back to Free, `api_access` returns to 0, and
-- existing keys stop authenticating with 402 `plan_required`. Keys are NOT
-- revoked — upgrading restores them instantly.
UPDATE plans SET api_access_enabled = 1 WHERE code = 'pilot';

UPDATE entitlements e SET api_access = 1
  FROM plans p WHERE p.code = e.plan_code AND p.api_access_enabled = 1;
