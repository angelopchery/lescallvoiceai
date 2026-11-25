-- Rollback 0014: drop the staff Enterprise grants back to 0013's (broken)
-- state. Kept for symmetry with the other migrations; there is no reason to
-- run it except while testing the migration itself.

UPDATE entitlements
   SET overrides = NULL,
       updated_at = now(),
       updated_by = 'migration:0014.down'
 WHERE owner IN ('tech@dhronai.com', 'angelo@dhronai.com',
                 'alwynantonyben@dhronai.com');
