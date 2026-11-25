-- 0009: billing transparency for platform-initiated calls.
--
-- The follow-up auto-dialer and the automation engine place real outbound
-- calls that meter exactly like human-clicked ones (usage_ledger billsec).
-- Nothing marked WHO initiated a call, so a tenant could not audit
-- platform-initiated spend. NULL = pre-0009 rows (origin unknown).
--   user      — a human clicked Call (outbound) or the customer dialled in
--   autodial  — the follow-up executor placed it
--   automation— an automation rule's dial action placed it
ALTER TABLE calls ADD COLUMN placed_by TEXT;

-- The origin is declared where the dial is placed (_record_dial) and travels
-- to the calls row via the bridge's outbound dial match — the bridge never
-- guesses. followup_id alone can't distinguish automation dials from manual.
ALTER TABLE call_links ADD COLUMN placed_by TEXT;
