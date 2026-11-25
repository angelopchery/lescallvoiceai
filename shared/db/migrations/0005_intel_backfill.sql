-- 0005: Legacy follow-up data → the new followups table (data-only).
--
-- Sources being retired (tables left untouched for rollback):
--   * follow_ups: 'dismissed' status-overrides of old analysis entries are
--     NOT migrated — every historical call has been re-decided by the new
--     engine, so those overrides are obsolete. The one live manual entry
--     migrates as a manual follow-up.
--   * followup_automations: 'scheduled' rows with no scheduled_time were
--     stale test entries for the retired auto-dialer; they migrate as
--     pending manual follow-ups so nothing a user typed is lost.
--
-- Runs exactly once via schema_migrations, so plain INSERT..SELECT is safe.

INSERT INTO followups (owner, contact_id, decision, confidence, explanation,
                       reason, summary, followup_type, scheduled_at, priority,
                       status, source, audit)
SELECT f.owner,
       NULL,
       'yes', 1.0,
       'Migrated from the legacy manual follow-ups store',
       COALESCE(NULLIF(f.note::jsonb->>'reason', ''),
                'Manual follow-up for ' || COALESCE(f.note::jsonb->>'contact_name',
                                                    f.contact_phone, 'contact')),
       f.note::jsonb->>'contact_phone',
       'callback',
       f.due_at,
       COALESCE(NULLIF(f.priority, ''), 'medium'),
       CASE WHEN f.status IN ('pending', 'scheduled') THEN 'pending'
            ELSE 'archived' END,
       'manual',
       '[{"by": "migration-0005", "action": "migrated from follow_ups"}]'
  FROM follow_ups f
 WHERE f.source = 'manual_followups.json';

INSERT INTO followups (owner, decision, confidence, explanation, reason,
                       summary, followup_type, scheduled_at, priority, status,
                       source, audit)
SELECT a.owner,
       'yes', 1.0,
       'Migrated from the legacy follow-up automation store',
       COALESCE(NULLIF(a.call_aim, ''),
                'Callback ' || COALESCE(NULLIF(a.contact_name, ''),
                                        a.contact_phone, 'contact')),
       a.contact_phone,
       'callback',
       a.scheduled_time,
       'medium',
       CASE WHEN a.status = 'scheduled' AND a.scheduled_time IS NOT NULL
            THEN 'scheduled' ELSE 'pending' END,
       'manual',
       '[{"by": "migration-0005", "action": "migrated from followup_automations"}]'
  FROM followup_automations a
 WHERE a.status IN ('scheduled', 'calling');
