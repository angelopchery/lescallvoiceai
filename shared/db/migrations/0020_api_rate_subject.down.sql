DELETE FROM api_rate_windows WHERE subject LIKE 'acct:%';
UPDATE api_rate_windows SET subject = substring(subject from 5)
 WHERE subject LIKE 'key:%';
ALTER TABLE api_rate_windows RENAME COLUMN subject TO key_id;
