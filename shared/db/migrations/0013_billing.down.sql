-- Rollback for 0013_billing.sql.
--
-- Drop order is child -> parent so the foreign keys never block a drop.
-- payment_attempts and minute_topups reference payments and subscriptions;
-- entitlements and subscriptions reference plans.
--
-- This DESTROYS billing history. It is a testing/rollback tool for a phase
-- that has taken no real money yet. Once a live payment exists, roll forward
-- with a new migration instead — an issued GST invoice may not be deleted.

DROP TABLE IF EXISTS payment_attempts;
DROP TABLE IF EXISTS billing_reservations;
DROP TABLE IF EXISTS minute_topups;
DROP TABLE IF EXISTS billing_events;
DROP TABLE IF EXISTS receipt_counters;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS entitlements;
DROP TABLE IF EXISTS pilot_claims;
DROP TABLE IF EXISTS subscriptions;
DROP TABLE IF EXISTS plans;

DELETE FROM settings WHERE owner = '' AND key = 'billing_enforce';
