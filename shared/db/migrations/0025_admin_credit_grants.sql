-- 0025 — admin-granted minute credits ("rewards"), on the existing top-up rail.
--
-- An operator needs to hand a tenant free minutes: onboarding sweeteners, an
-- apology for an outage, a negotiated pilot extension. That is the SAME thing
-- a paid top-up already is — extra minutes on top of the plan's allowance for
-- a bounded window — so it reuses `minute_topups` rather than inventing a
-- second pool that every meter, gate and invoice would have to learn about.
-- `get_entitlements()` already folds active top-ups into `minutes_limit`, so
-- a grant takes effect on the very next call with no other code change.
--
-- What is genuinely new is VALIDITY IN MONTHS, and it is the reason for
-- `minutes_consumed`.
--
-- A paid top-up expires with the billing period that sold it, so "add N to
-- this period's ceiling" was always exact. A grant valid for THREE months is
-- not that: `minutes_limit = included + N` is recomputed every period, so a
-- 1000-minute / 3-month reward would silently hand out 3000 minutes — the
-- pool would refresh at every rollover. A reward has to be a POOL that drains.
--
--   minutes           what the admin granted; never changes
--   minutes_consumed  how much of the pool CLOSED periods already ate
--   settled_through   the end of the last calendar month settled
--
-- `shared/billing/credits.settle_owner()` (run by the billing sync worker)
-- walks the closed IST calendar months in the grant's window, and for each one
-- charges `max(0, minutes_used - minutes_included)` — the tenant's overspend
-- beyond their own plan — against the oldest grant still covering that month.
-- The live period needs no settlement: its overspend is already bounded by
-- `included + remaining`, which is what `get_entitlements()` now sums.
--
-- Grants younger than a calendar month therefore behave EXACTLY like a paid
-- top-up and cost nothing extra to read — the common case stays on the old
-- fast path.

ALTER TABLE minute_topups ADD COLUMN source           text NOT NULL DEFAULT 'razorpay';
ALTER TABLE minute_topups ADD COLUMN granted_by       text;
ALTER TABLE minute_topups ADD COLUMN note             text;
ALTER TABLE minute_topups ADD COLUMN minutes_consumed integer NOT NULL DEFAULT 0;
ALTER TABLE minute_topups ADD COLUMN settled_through  timestamptz;

-- Everything that exists today came from the gateway. Stated explicitly rather
-- than left to the column default, so a later reader of this table never has
-- to date-check a row to know where it came from.
UPDATE minute_topups SET source = 'razorpay' WHERE source IS NULL OR source = '';

-- The settlement pass asks for "active grants that still have a pool", and
-- get_entitlements() asks for "active top-ups for this owner" on the dial hot
-- path. One partial index serves both.
CREATE INDEX IF NOT EXISTS idx_topups_live
    ON minute_topups (owner, period_end)
    WHERE status = 'active';
