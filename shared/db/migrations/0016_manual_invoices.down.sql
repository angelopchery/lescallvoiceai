-- Rollback 0016. Drops manual-invoice bookkeeping; gateway payments are
-- unaffected because their money facts live in the original columns.
DROP INDEX IF EXISTS idx_payments_outstanding;
DROP INDEX IF EXISTS idx_payments_issued;
ALTER TABLE payments DROP COLUMN IF EXISTS void_reason;
ALTER TABLE payments DROP COLUMN IF EXISTS voided_at;
ALTER TABLE payments DROP COLUMN IF EXISTS created_by;
ALTER TABLE payments DROP COLUMN IF EXISTS payment_reference;
ALTER TABLE payments DROP COLUMN IF EXISTS paid_at;
ALTER TABLE payments DROP COLUMN IF EXISTS due_date;
ALTER TABLE payments DROP COLUMN IF EXISTS issued_at;
