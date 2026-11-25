DROP INDEX IF EXISTS idx_copilot_tokens_day;
DROP TABLE IF EXISTS copilot_token_usage;
ALTER TABLE entitlements DROP COLUMN IF EXISTS copilot_tokens_per_day;
ALTER TABLE plans        DROP COLUMN IF EXISTS copilot_tokens_per_day;
