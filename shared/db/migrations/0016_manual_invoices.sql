-- 0016_manual_invoices.sql — invoices collected outside the payment gateway.
--
-- Not every rupee arrives through Razorpay. Cash, NEFT/RTGS, a cheque, or an
-- account put on credit ("yet to be paid") all need a real GST tax invoice,
-- and the customer needs to see it in their own Billing page exactly like a
-- card payment.
--
-- Design decisions behind these columns (owner-approved 2026-08-01):
--
--  * An unpaid invoice is a REAL tax invoice, numbered from the same
--    consecutive LC-<fy>-NNNNN series on the day it is issued. Under GST the
--    invoice is raised at supply, not at collection, and it is reported in
--    that period's GSTR-1 whether or not the money has landed. If it is never
--    paid it is cancelled with a credit note — never deleted, never a gap in
--    the series.
--  * Issuing an unpaid invoice ACTIVATES the plan immediately. That is what
--    extending credit means; the balance shows as outstanding until settled.
--  * `created_at` stays the row's insert time. The human-meaningful invoice
--    date is `issued_at`, which an operator may set (a payment received last
--    Tuesday should be invoiced as last Tuesday).

ALTER TABLE payments ADD COLUMN issued_at TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN due_date TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN paid_at TIMESTAMPTZ;
-- UTR for NEFT/RTGS, cheque number, or whatever the operator can reconcile
-- against a bank statement later.
ALTER TABLE payments ADD COLUMN payment_reference TEXT;
ALTER TABLE payments ADD COLUMN created_by TEXT;
ALTER TABLE payments ADD COLUMN voided_at TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN void_reason TEXT;

-- Existing rows are all gateway captures: the money arrived when the row did.
UPDATE payments SET issued_at = created_at, paid_at = created_at
 WHERE issued_at IS NULL AND status = 'captured';
UPDATE payments SET issued_at = created_at WHERE issued_at IS NULL;

-- `status` gains two values beyond captured|failed|refunded:
--   due   — invoice issued, money not yet received (on credit)
--   void  — cancelled; a credit note reverses it
-- Left as free TEXT to match the rest of this schema, but documented here so
-- the vocabulary is discoverable.

-- The collections worklist: everything issued and not yet settled.
CREATE INDEX idx_payments_outstanding ON payments(owner, due_date)
    WHERE status = 'due';
CREATE INDEX idx_payments_issued ON payments(issued_at);
