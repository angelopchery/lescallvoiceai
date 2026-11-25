-- 0020: the rate limiter counts SUBJECTS, not only keys.
--
-- 0019 keyed every window on api_keys.key_id, which made the account's rate
-- ceiling advisory rather than a ceiling: a tenant allowed 60 req/min could
-- mint five keys and take 300, because each key carried its own private
-- counter. The limiter now opens a second window per ACCOUNT alongside the
-- per-key one, so the column holds either kind of subject:
--
--     key:<key_id>        one integration's own budget
--     acct:<owner email>  everything that account is doing, across all keys
--
-- Renamed rather than overloaded: a column called key_id holding "acct:…" is
-- the kind of quiet lie that makes the next reader's mental model wrong.
ALTER TABLE api_rate_windows RENAME COLUMN key_id TO subject;

-- Existing rows are per-key counters written under the old naming; re-prefix
-- them so the new reader finds them instead of silently starting from zero
-- (which would hand every live integration a fresh budget at deploy time).
UPDATE api_rate_windows SET subject = 'key:' || subject
 WHERE subject NOT LIKE 'key:%' AND subject NOT LIKE 'acct:%';
