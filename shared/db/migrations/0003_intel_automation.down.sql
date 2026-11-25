-- Rollback of 0003_intel_automation (testing environments only).
DROP TABLE IF EXISTS automation_runs;
DROP TABLE IF EXISTS automation_rule_versions;
DROP TABLE IF EXISTS automation_rules;
