-- 0014_billing_staff_overrides.sql — fix the staff Enterprise seed.
--
-- 0013 seeded the three staff tenants with entitlements.plan_code='enterprise'
-- but no `overrides`. That does not survive: `resolve_entitlements()` rebuilds
-- the row wholesale from the `subscriptions` history, and these accounts have
-- no subscription (they never paid — they are staff), so the first resolve
-- correctly reset them to Free. Verified live: all three dropped to
-- free/30 minutes/1 agent the moment the P6 seed pass ran.
--
-- `overrides` is the supported way to grant a plan without a gateway
-- subscription — it is what the admin plane writes, and resolve_entitlements()
-- gives it precedence over the derived plan. So the grant has to live there.
--
-- Not folded into 0013 because 0013 is already applied on the live database
-- and the migration runner aborts on checksum drift, by design.

UPDATE entitlements
   SET overrides = '{"plan_code": "enterprise"}',
       plan_code = 'enterprise',
       minutes_limit = NULL,
       minutes_included = NULL,
       max_agent_families = NULL,
       max_concurrent_calls = NULL,
       copilot_turns_per_day = NULL,
       recordings = 1, summaries = 1, transcripts = 1, intelligence = 1,
       conversation_analytics = 1, advanced_analytics = 1, autocall = 1,
       status = 'ok',
       updated_at = now(),
       updated_by = 'migration:0014'
 WHERE owner IN ('tech@dhronai.com', 'angelo@dhronai.com',
                 'alwynantonyben@dhronai.com');

-- Everyone else who already exists gets an EXPLICIT Free row, so the P6
-- discovery query returns zero rows and nobody's plan is implicit. Free is
-- what they are entitled to; making it explicit is what makes it deliberate.
INSERT INTO entitlements (owner, plan_code, minutes_limit, minutes_included,
                          max_agent_families, max_concurrent_calls,
                          copilot_turns_per_day, status, updated_by, notes)
SELECT DISTINCT t.owner, 'free', 30, 30, 1, 1, 20, 'ok', 'migration:0014',
       'pre-existing account, explicit Free at billing rollout'
  FROM (SELECT email AS owner FROM users
        UNION SELECT owner FROM agents
        UNION SELECT owner FROM usage_ledger
        UNION SELECT owner FROM calls) t
 WHERE t.owner IS NOT NULL AND t.owner <> ''
   AND t.owner NOT IN (SELECT owner FROM entitlements)
ON CONFLICT (owner) DO NOTHING;
