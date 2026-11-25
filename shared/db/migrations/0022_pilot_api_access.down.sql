UPDATE plans SET api_access_enabled = 0 WHERE code = 'pilot';
UPDATE entitlements SET api_access = 0 WHERE plan_code = 'pilot';
