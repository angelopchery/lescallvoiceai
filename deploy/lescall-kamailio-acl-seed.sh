#!/usr/bin/env bash
# Boot-time self-heal for the Kamailio carrier ACL (plan §4c).
#
# If /etc/kamailio/kamailio.db's address table is ever empty (fresh DB, failed
# migration), every carrier INVITE is silently 403'd and inbound looks dead.
# This script rebuilds address grp-1 from trunks.provider_ips in the app DB,
# then retries a live reload until Kamailio's ctl socket is up.
#
# Dependency-free: system python3 + stdlib sqlite3 only.
# Installed by deploy/lescall-kamailio-acl-seed.service (runs at boot).

set -u

APP_DB="${LESCALL_APP_DB:-/home/angelo/lescall/shared/db/lescall.db}"
KAM_DB="${KAMAILIO_DB_PATH:-/etc/kamailio/kamailio.db}"
KAMCMD="${KAMCMD:-/usr/sbin/kamcmd}"
ACL_GROUP="${KAMAILIO_ACL_GROUP:-1}"
RETRIES="${SEED_RELOAD_RETRIES:-30}"

python3 - "$APP_DB" "$KAM_DB" "$ACL_GROUP" <<'PYEOF'
import ipaddress, sqlite3, sys

app_db, kam_db, grp = sys.argv[1], sys.argv[2], int(sys.argv[3])

cidrs = []
try:
    with sqlite3.connect(app_db, timeout=10) as conn:
        rows = conn.execute(
            "SELECT provider_ips FROM trunks WHERE provider_ips IS NOT NULL").fetchall()
    for (ips,) in rows:
        cidrs.extend(p.strip() for p in str(ips).split(",") if p.strip())
except Exception as exc:
    print(f"acl-seed: app DB unreadable ({exc!r}); leaving Kamailio ACL untouched")
    sys.exit(0)  # do NOT wipe a possibly-good ACL when the source is unreadable

parsed, seen = [], set()
for item in cidrs:
    try:
        net = ipaddress.ip_network(item, strict=False)
        key = (str(net.network_address), net.prefixlen)
        if key not in seen:
            seen.add(key)
            parsed.append(key)
    except ValueError:
        print(f"acl-seed: skipping invalid CIDR {item!r}")

if not parsed:
    print("acl-seed: no provider CIDRs in trunks table; leaving ACL untouched")
    sys.exit(0)

with sqlite3.connect(kam_db, timeout=10) as conn:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS address (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " grp INTEGER DEFAULT 1 NOT NULL, ip_addr VARCHAR(50) NOT NULL,"
        " mask INTEGER DEFAULT 32 NOT NULL, port SMALLINT DEFAULT 0 NOT NULL,"
        " tag VARCHAR(64))")
    conn.execute("DELETE FROM address WHERE grp=?", (grp,))
    for ip, mask in parsed:
        conn.execute(
            "INSERT INTO address (grp, ip_addr, mask, port, tag) VALUES (?,?,?,0,'carrier')",
            (grp, ip, mask))
print(f"acl-seed: wrote {len(parsed)} carrier networks to grp {grp}")
PYEOF

status=$?
[ $status -ne 0 ] && exit $status

# Retry the live reload until Kamailio's ctl socket answers (boot race).
for i in $(seq 1 "$RETRIES"); do
    if "$KAMCMD" permissions.addressReload >/dev/null 2>&1; then
        echo "acl-seed: addressReload ok (attempt $i)"
        exit 0
    fi
    sleep 2
done
echo "acl-seed: WARNING kamcmd unreachable after $RETRIES attempts; ACL is in DB but not live-loaded"
exit 0
