-- Kamailio 5.x permissions-module tables (SQLite dialect) for
-- /etc/kamailio/kamailio.db. Apply once at install:
--   sqlite3 /etc/kamailio/kamailio.db < kamailio/schema-sqlite.sql
-- The `address` grp-1 rows are managed at runtime by shared/kamailio_acl.py
-- and re-seeded at boot by deploy/lescall-kamailio-acl-seed.sh.

CREATE TABLE IF NOT EXISTS version (
    table_name VARCHAR(32) NOT NULL PRIMARY KEY,
    table_version INTEGER DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS address (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grp INTEGER DEFAULT 1 NOT NULL,
    ip_addr VARCHAR(50) NOT NULL,
    mask INTEGER DEFAULT 32 NOT NULL,
    port SMALLINT DEFAULT 0 NOT NULL,
    tag VARCHAR(64)
);
INSERT OR IGNORE INTO version (table_name, table_version) VALUES ('address', 6);

CREATE TABLE IF NOT EXISTS trusted (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_ip VARCHAR(50) NOT NULL,
    proto VARCHAR(4) NOT NULL,
    from_pattern VARCHAR(64),
    ruri_pattern VARCHAR(64),
    tag VARCHAR(64),
    priority INTEGER DEFAULT 0 NOT NULL
);
INSERT OR IGNORE INTO version (table_name, table_version) VALUES ('trusted', 6);
