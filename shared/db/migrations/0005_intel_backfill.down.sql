-- Rollback of 0005_intel_backfill (testing environments only).
DELETE FROM followups WHERE audit LIKE '%migration-0005%';
