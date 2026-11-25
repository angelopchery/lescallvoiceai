-- 0021: the integration API becomes a GROWTH capability.
--
-- Follows the exact shape of every other marketed capability (0013 §Catalogue):
-- one flag on `plans`, one on `entitlements`, resolved by
-- entitlements.resolve_entitlements() and enforced by feature_allowed().
-- Adding it as a column rather than hard-coding `plan_code == 'growth'` is what
-- lets the admin plane grant it to a single Enterprise tenant, or to a customer
-- mid-negotiation, without a code change.

ALTER TABLE plans ADD COLUMN api_access_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entitlements ADD COLUMN api_access INTEGER NOT NULL DEFAULT 0;

-- Growth and Enterprise only. Deliberately NOT Pilot: a 7-day trial that ends
-- with a customer's CRM silently losing its feed is a worse first experience
-- than never having wired it up, and the trial is meant to sell the calls, not
-- the plumbing.
UPDATE plans SET api_access_enabled = 1 WHERE code IN ('growth', 'enterprise');

-- Existing entitlement rows are stale the moment a plan flag changes; they are
-- rebuilt wholesale by resolve_entitlements() on the next pass, but a tenant
-- who is already on Growth should not lose access in the meantime.
UPDATE entitlements e SET api_access = 1
  FROM plans p WHERE p.code = e.plan_code AND p.api_access_enabled = 1;

-- The staff tenants carry explicit Enterprise rows seeded in 0013; keep them
-- whole rather than relying on the join above having matched.
UPDATE entitlements SET api_access = 1 WHERE plan_code = 'enterprise';
