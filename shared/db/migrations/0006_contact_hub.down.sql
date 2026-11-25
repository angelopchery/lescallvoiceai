-- Rollback of 0006_contact_hub (testing environments only).
DROP INDEX IF EXISTS idx_commitments_contact;
DROP INDEX IF EXISTS idx_followups_contact;
DROP TABLE IF EXISTS contact_grants;
DROP TABLE IF EXISTS contact_events;
ALTER TABLE customer_memory DROP COLUMN IF EXISTS digest_updated_at;
ALTER TABLE customer_memory DROP COLUMN IF EXISTS context_digest;
