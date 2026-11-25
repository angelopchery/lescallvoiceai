# Multi-Tenant / BYON / Kamailio — Deploy Runbook

Companion to `docs/MULTITENANT_BYON_PLAN.md`. All code is in the repo; this is
the ordered list of **operational steps that were deliberately NOT executed
automatically** because they touch the live phone system. Steps are grouped by
blast radius. Nothing below is required for the system to keep working exactly
as it does today — every code path falls back to legacy behavior until these
steps are taken.

Live topology today (verified 2026-07-11):

- FreeSWITCH binds `10.160.0.8` — internal profile :5060, external profile
  :5080 (`vars.xml external_sip_port=5080`). Public IP `35.207.225.237` NATs to
  this host; Plivo origination therefore hits **:5080** directly.
- Inbound DID routing is the `plivo_inbound` extension **inline in
  `/etc/freeswitch/dialplan/public.xml`** with a `.*` catch-all, placed BEFORE
  the `public/*.xml` include — it shadows any per-DID file until removed.
- `/etc/freeswitch` is `freeswitch:freeswitch` mode 755 — the dashboard user
  (angelo, member of group `freeswitch`) cannot write it without step 2.
- Kamailio and Postgres are NOT installed.

---

## 0. Restart services to activate the new code (small window, ~10s)

The DID plumbing + per-call routing only take effect when the processes reload.
BOTH stacks are systemd-managed (`lescall-runtime.service` wraps
start_all/stop_all; `lescall-dashboard.service` Restart=always) — use
systemctl, NOT the raw scripts: launching via `start.sh --bg` while the unit
is enabled leaves systemd crash-looping against the occupied port (this
happened 2026-07-12; the fix was killing the manual process so the unit binds).

```bash
sudo systemctl restart lescall-runtime     # socket_server + ws_audio_receiver
sudo systemctl restart lescall-dashboard   # dashboard  [DONE 2026-07-12 — new code live]
```

Behavior after restart with the seeded DB: **identical to today** — the DID
+918065953160 is bound to the same slug `active_agent.json` points at, and
every failure path falls back to `active_agent.json`.

## 1. Phase 0 + 1 — database (NO downtime, already runnable)

```bash
cd /home/angelo/lescall
python3 -m shared.db.seed          # users, agents index, Plivo trunk, DID binding
python3 -m shared.db.import_json   # contacts / call analysis / follow-ups (files stay source of truth)
```

Both are idempotent; re-running changes nothing (seed's trunk/DID part is
bootstrap-only — it never resurrects operator-deleted rows). To undo entirely:
`rm shared/db/lescall.db*` (the code falls back to legacy resolution).

Notes: run `import_json` during a quiet period (its per-store transactions
briefly hold the sqlite write lock), and treat it as a **pre-go-live** tool —
once the bridge starts persisting calls rows, re-importing `call_analysis.json`
duplicates those calls (the report prints a warning when it detects this).

## 2. FS config write access for BYON "apply" (NO downtime)

The dashboard's `POST /api/trunks/{id}/apply` writes gateway + dialplan XML.
Give the dashboard user group-write on exactly the two generated-config dirs:

```bash
sudo chgrp freeswitch /etc/freeswitch/sip_profiles/external /etc/freeswitch/dialplan/public /etc/freeswitch/autoload_configs
sudo chmod g+w        /etc/freeswitch/sip_profiles/external /etc/freeswitch/dialplan/public /etc/freeswitch/autoload_configs
sudo chmod g+w        /etc/freeswitch/autoload_configs/acl.conf.xml   # Apply truncates it in place
```

Until this is done, "apply" returns a 500 with a hint pointing here.

**STATUS: DONE 2026-07-12** — verified end-to-end with a disposable trunk
through the live API: gateway XML + acl.conf.xml written, reloadxml/rescan/
reloadacl all +OK, stock `lan`/`domains` ACL lists preserved, test artifacts
removed afterward.

## 3. Per-DID dialplan cutover (SHORT window; rollback = one file edit)

Goal: replace the `.*` catch-all with generated per-DID extensions so a second
tenant's DID can route to a different agent.

1. `POST /api/trunks/{id}/apply` (or run `shared/freeswitch_config.apply_config`
   manually) → writes `dialplan/public/ai_inbound_8065953160.xml`.
2. Confirm the generated file's regex matches the DID forms you see in logs.
3. Edit `/etc/freeswitch/dialplan/public.xml`: comment out the whole
   `plivo_inbound` extension (it currently shadows the include).
4. `fs_cli -x reloadxml`, place a test call.
5. **Rollback:** uncomment `plivo_inbound`, `reloadxml`.

Note: keep `apply-inbound-acl` OFF until Kamailio (step 4) — today Plivo talks
to FS directly and an ACL would block it.

## 4. Kamailio SIP edge (MAINTENANCE WINDOW; inbound is down mid-change)

Decision baked into the prepared configs: **Kamailio takes public :5060; the
carrier origination URI moves from :5080 to :5060.** (Alternative — Kamailio on
5080 and FS external moved — requires an FS port change AND a Plivo change;
strictly worse.)

```bash
# a. install (kamailio + sqlite backend)
sudo apt-get install -y kamailio kamailio-extra-modules kamailio-sqlite-modules

# b. seed Kamailio's own DB (address/trusted tables)
sudo sqlite3 /etc/kamailio/kamailio.db < /home/angelo/lescall/kamailio/schema-sqlite.sql

# c. config
sudo cp /home/angelo/lescall/kamailio/kamailio.cfg   /etc/kamailio/kamailio.cfg
sudo cp /home/angelo/lescall/kamailio/dispatcher.list /etc/kamailio/dispatcher.list
sudo kamailio -c -f /etc/kamailio/kamailio.cfg        # syntax check BEFORE starting

# d. free :5060 on the private IP — FS *internal* profile holds it (unused for
#    carrier traffic). In /etc/freeswitch/vars.xml set internal_sip_port=5062,
#    then: fs_cli -x 'sofia profile internal restart'
#    (Kamailio's own FS-facing internal socket is :5064 — see kamailio.cfg —
#    so the ports never collide: Kamailio 5060+5064, FS 5062+5080.)

# e. start the edge
sudo systemctl enable --now kamailio
sudo cp /home/angelo/lescall/deploy/lescall-kamailio-acl-seed.service /etc/systemd/system/
sudo systemctl enable --now lescall-kamailio-acl-seed.service
sudo mkdir -p /etc/systemd/system/kamailio.service.d
sudo cp /home/angelo/lescall/deploy/kamailio.service.d-10-lescall-resilience.conf \
        /etc/systemd/system/kamailio.service.d/10-lescall-resilience.conf
sudo systemctl daemon-reload

# e2. give the dashboard a working live-ACL path — WITHOUT this, trunk applies
#     write the app DB but the Kamailio ACL silently lags until next boot:
sudo chgrp freeswitch /etc/kamailio/kamailio.db && sudo chmod g+w /etc/kamailio/kamailio.db /etc/kamailio
#     (angelo is in group freeswitch; or create a dedicated group)
#     AND install the trigger-file fallback units so reloads work even if the
#     kamcmd ctl socket stays root-only:
sudo cp /home/angelo/lescall/kamailio/systemd/lescall-kamailio-reload.{path,service} /etc/systemd/system/
sudo systemctl enable --now lescall-kamailio-reload.path

# f. carrier ACL: put Plivo's signaling CIDRs into the trunk's provider_ips via
#    the dashboard (PATCH is not needed — edit + re-apply), or directly:
#    kamcmd permissions.dumpAddresses   # to verify after sync

# g. lock FS behind the proxy: in sip_profiles/external.xml set
#      <param name="apply-inbound-acl" value="trusted_kamailio"/>
#    ensure autoload_configs/acl.conf.xml exists (any dashboard "apply" writes
#    it, or render_acl manually) — it ALWAYS contains trusted_kamailio
#    (127.0.0.1 + KAMAILIO_IP env, default matches 10.160.0.8 via generated file).
#    Set env KAMAILIO_IP=10.160.0.8 for the dashboard process before applying.
#    fs_cli -x 'reloadacl' ; fs_cli -x 'sofia profile external rescan'

# h. carrier: change Plivo INBOUND zentrunk origination URI to
#    sip:35.207.225.237:5060  (was :5080)

# i. test: inbound call → kamailio log shows relay → FS answers → agent speaks.
```

**Rollback:** point Plivo origination back at :5080, remove
`apply-inbound-acl`, `systemctl stop kamailio`. FS keeps working as before.

GCP note: open UDP 5060 to the carrier IP ranges in the VPC firewall (5080 can
be closed to the world after cutover). RTP range stays as-is (media is direct
carrier↔FS).

## 5. Google OAuth (login page + login wall)

Single-tenant fallback is active until these are set (they can live in
`calling-agent/.env` — auth.py loads it — or the dashboard process env):

```
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
OAUTH_REDIRECT_URL=https://agent.lescall.com/auth/callback
LESCALL_SESSION_SECRET=<long random string>     # else dashboard/.session_secret is generated
LESCALL_ALLOWED_EMAILS=tech@dhronai.com         # comma-separated; "*" = open signup
```

Google Cloud Console prerequisites (console.cloud.google.com → APIs & Services
→ Credentials → Create OAuth client ID, type "Web application"):
- Authorized redirect URI: `https://agent.lescall.com/auth/callback`
  (add `http://localhost:8080/auth/callback` for local testing)
- Configure the OAuth consent screen (app name "LesCall", logo, support email).

What turns on the moment BOTH client id + secret are set (dashboard restart):
- **Login wall** (`auth.install_login_gate`): every route except `/login`,
  `/auth/*`, `/static/*`, `/api/auth/me` requires a session — HTML navigations
  redirect to the branded login page (`static/login.html`, styled after
  lescall.com), API calls get 401.
- `/auth/login` → Google → `/auth/callback` verifies + checks
  `LESCALL_ALLOWED_EMAILS`, upserts the `users` row, sets the signed cookie.
- `/auth/logout` clears it.

KEEP the allow-list at trusted emails until the legacy endpoints are
owner-scoped — any allow-listed account currently sees the whole dashboard.
The cookie `Secure` flag follows OAUTH_REDIRECT_URL's scheme, so use the
https URL in production (TLS terminates in front of nginx).

**STATUS: DONE 2026-07-12** — creds in `calling-agent/.env`, dashboard
restarted, live on https://agent.lescall.com.

**DOMAIN MOVE 2026-07-25**: agent.lescall.dhronai.com → **agent.lescall.com**
(dhronai.com DNS records deleted). New Let's Encrypt cert issued; the legacy
dhronai vhosts and their certs were fully removed the same day (certbot
delete + dashboard.lescall vhost dropped — the old names no longer resolve).
Google OAuth client: redirect URI + JS origin updated to agent.lescall.com in
the console. Stored `calls.recording_url` links were rewritten in the DB.
Topology note: apex `lescall.com`/`www` stay on the separate marketing box
(35.207.233.208); only `agent.lescall.com` points at this VM.

Host-nginx layer (required because agent.lescall proxies "/" to the SPA
*container* on :8081, which would bypass FastAPI's gate): the vhosts in
`deploy/nginx-agent.lescall.conf` + `deploy/nginx-dashboard.lescall.conf` are
installed at `/etc/nginx/sites-enabled/{agent,dashboard}.lescall` (originals
in `/etc/nginx/lescall-backups/`). They send `/auth/`, `/login`, `/api/`,
`/static/` straight to :8080 and put an `auth_request` probe
(`/api/auth/me`) in front of the container SPA — a no-op whenever OAuth is
unconfigured. Rollback: restore the backups + `nginx -t && systemctl reload nginx`.

(Historical caveat, resolved by the 2026-07-25 domain move: DNS for
`dashboard.lescall.dhronai.com` pointed at a different old server; that
record is deleted along with the rest of the dhronai.com zone.)

## 5b. Number-verification dialplan (outbound caller-ID proof) — DONE 2026-07-12

Outbound calls only use a caller ID the tenant has VERIFIED (proved control of
by answering an OTP call) AND that egresses through the tenant's OWN applied
trunk gateway (`sofia/gateway/<name>/…`, so their carrier authorises the
caller-ID + billing) AND uses an agent the tenant OWNS — else the call is
REFUSED. The verification call speaks a code via this static dialplan:

```bash
sudo cp /home/angelo/lescall/deploy/freeswitch-lescall-verify.xml \
        /etc/freeswitch/dialplan/default/lescall_verify.xml
sudo fs_cli -x reloadxml    # needs mod_say_en (default-loaded)
```

Installed on this host. Requires each tenant's trunk to be **applied** (so its
FreeSWITCH gateway exists) before verify/dial works. Admins bypass the verified
gate; the agent-ownership gate applies to everyone (closes the "another
tenant's agent answered my call" leak).

## 6. Outbox worker (client-ERP lead hand-off)

```
LESCALL_ERP_WEBHOOK_URL=https://...   # unset = log-only mode (safe default)
LESCALL_ERP_WEBHOOK_TOKEN=...         # optional bearer
python3 -m shared.outbox_worker --interval 30   # run under systemd/tmux
```

Enqueue from any code: `shared.outbox.enqueue(payload, owner=...)` — same lead
enqueued twice stays one row; delivery retries with backoff; carrier "duplicate"
responses are terminal-success.

## 6b. Call recordings → GCS + public download links (2026-07-13)

Every finished call writes a full `calls` row (owner, agent_slug, callee
phone, duration_s, transcript, summary, collected_data slots, follow_up from
AI analysis, recording_path) plus a **256-bit random `download_token`**. The
public link is `https://agent.lescall.com/api/recordings/dl/<token>`
— auth-exempt, unguessable, and any miss returns an identical 404, so a
leaked link cannot be edited into someone else's recording.

Pieces:
- `shared/recording_storage.py` — token mint, GCS upload, catch-up sync,
  local retention cleanup. CLI: `python -m shared.recording_storage --once`.
- Bridge uploads the WAV right after call end; the
  `lescall-recsync.timer` (15 min, installed 2026-07-13) retries misses,
  backfills tokens, and deletes local WAVs `RECORDING_LOCAL_RETENTION_DAYS`
  (default 3) days after they're safely in GCS. Playback/download endpoints
  fall back to streaming from GCS when the local file is gone.
- Config in `deploy/.pg_env` (both services + timer read it).

### GCS setup — DONE 2026-07-14
- Bucket `gs://lescall-recordings` (asia-south1, uniform access, PRIVATE —
  zero `allUsers` grants; downloads always go through the tokenized endpoint).
- SA `lescall-recsync@lescall.iam.gserviceaccount.com` with
  `roles/storage.objectAdmin` scoped to the bucket only.
- Backlog of 21 recordings uploaded; GCS-fallback streaming verified on the
  public link after local deletion.

### Auth: attached service account, NOT a login (durable fix, 2026-07-31)

**Doctrine: a production VM must never authenticate as a human.** The first
build used impersonated ADC — `gcloud auth application-default login
--impersonate-service-account=lescall-recsync@…` — because the org policy
`iam.disableServiceAccountKeyCreation` blocks JSON *and* HMAC key files. That
credential rides on alwynantonyben@'s Google session, so org session control
expired it on **2026-07-14** and every sync since died with `RefreshError:
Reauthentication is needed`. By 07-31: 270 of 311 calls local-only, log
shipping dead on the same credential, and the 41 calls that *had* uploaded
were unservable — retention had deleted their local copies and the GCS
fallback needs the same dead token. Re-running the login is a 90-day snooze
on the same outage, not a fix.

The durable answer is the third option GCE gives you, between "key file"
(blocked) and "human session" (expires): **attach the SA to the instance and
let the metadata server mint tokens.** No key material exists, nothing to
rotate, nothing to renew — tokens refresh for the life of the VM.

Both the SA and its scopes are boot-time properties, so this needs the
instance **STOPPED** (~2-3 min of inbound-call downtime — schedule it). The
external IP `agent` 35.207.225.237 is a *reserved static* address and
survives the stop, so the Plivo trunk ACL needs no change.

Run from Cloud Shell or any authed admin machine — deliberately NOT from the
VM, which is the whole point:

```bash
SA=lescall-recsync@lescall.iam.gserviceaccount.com

# 1. Roles the metadata SA must carry. objectAdmin on lescall-recordings is
#    already granted; the other three replace what the default compute SA
#    was doing (log shipping + the Cloud Ops agent, which is enabled here —
#    forget these and ops-agent telemetry goes dark at the same moment).
gcloud storage buckets add-iam-policy-binding gs://lescall-logs \
  --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
gcloud projects add-iam-policy-binding lescall \
  --member="serviceAccount:$SA" --role=roles/logging.logWriter
gcloud projects add-iam-policy-binding lescall \
  --member="serviceAccount:$SA" --role=roles/monitoring.metricWriter

# 2. Swap the SA (VM must be stopped for this API call to be accepted).
gcloud compute instances stop  lescall-agent --zone asia-south1-c
gcloud compute instances set-service-account lescall-agent \
  --zone asia-south1-c --service-account "$SA" --scopes cloud-platform
gcloud compute instances start lescall-agent --zone asia-south1-c
```

`--scopes cloud-platform` is required, not cosmetic: legacy scopes cap IAM,
and the default compute SA sat at `devstorage.read_only`, which silently
downgrades every write no matter what roles you grant.

### The two credential-precedence traps (both bite AFTER the stop/start)

`google.auth.default()` resolves in a fixed order and metadata is **last**:
`GOOGLE_APPLICATION_CREDENTIALS` → `~/.config/gcloud/application_default_
credentials.json` → metadata server. So attaching the SA changes nothing on
its own if either of the first two still resolves. There are two distinct
consumers and each is blocked by a different one:

**Containers** are blocked by the env var — and `restart: unless-stopped`
does NOT re-read the compose file. On VM boot Docker restarts the *existing*
container objects with the environment baked in at creation time (verified
2026-07-31: all three still carried the dead `GOOGLE_APPLICATION_CREDENTIALS`
from their July create dates). The compose edit only lands when the
containers are **recreated**:

```bash
docker compose -f deploy/docker-compose.services.yml up -d   # recreates api/runtime/workers
```

**Host processes** (`lescall-sentinel.service`, `lescall-recsync.timer`) set
no env var, so they fall through to the well-known ADC path and keep using
the stale user token until the file is gone:

```bash
rm -f ~/.config/gcloud/application_default_credentials.json
```

Do the `rm` **after** container-side GCS is verified, not before — while the
file still exists, `git checkout deploy/docker-compose.services.yml && docker
compose up -d` is a complete app-layer rollback with no VM stop.

### Cutover order

1. `gcloud compute instances stop / set-service-account / start` (above).
2. On the VM: `docker compose -f deploy/docker-compose.services.yml up -d`.
3. Verify metadata identity + a real GCS round-trip (below).
4. `rm -f ~/.config/gcloud/application_default_credentials.json`.
5. Confirm host-side too: `journalctl -u lescall-sentinel -n 30` shows no
   `gcs-auth` alert on the next hourly check.

### Verify

```bash
# 1. identity — must be lescall-recsync@, NOT 927969746216-compute@
curl -H Metadata-Flavor:Google \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email
# 2. scopes — must include cloud-platform
curl -H Metadata-Flavor:Google \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/scopes
# 3. no stale env survived the recreate — must print (unset)
docker exec lescall-workers printenv GOOGLE_APPLICATION_CREDENTIALS || echo "(unset)"
# 4. WRITE round-trip (read alone proves nothing — read_only scope was the bug)
docker exec lescall-workers python -c "
from google.cloud import storage
b = storage.Client().bucket('lescall-recordings')
b.blob('.healthcheck').upload_from_string('ok'); b.blob('.healthcheck').delete()
print('GCS write OK')"
# 5. drain the backlog (~270 calls), then confirm the DB stamps moved
docker exec lescall-workers python -m shared.recording_storage --once
docker exec lescall-pg psql -U lescall -d lescall -c \
  "SELECT count(*) total, count(gcs_uploaded_at) uploaded, max(gcs_uploaded_at) FROM calls;"
```

Step 4 is the one that matters: a `list_blobs` read succeeds under
`devstorage.read_only` too, so only a write proves the scope change took.

Watch disk during the drain — `recordings/` is ~1.5 GB. Nothing was lost in
the 17-day outage because `cleanup_local` only prunes what is confirmed in
the bucket.

### Rollback

The cutover cannot destroy recordings: local WAVs are deleted only after a
successful upload is stamped, so the worst case is uploads stay broken
exactly as they are today.

- **App layer (no VM stop, seconds):** `git checkout
  deploy/docker-compose.services.yml deploy/.pg_env && docker compose -f
  deploy/docker-compose.services.yml up -d` — restores the env var and the
  gcloud mount. Only works while the ADC file still exists, which is why the
  `rm` is step 4 and not step 2.
- **Full SA revert (needs another stop, ~3 min):** re-attach the default
  compute SA with its original scope set, captured 2026-07-31 before the
  cutover:

```bash
gcloud compute instances stop lescall-agent --zone asia-south1-c
gcloud compute instances set-service-account lescall-agent \
  --zone asia-south1-c \
  --service-account 927969746216-compute@developer.gserviceaccount.com \
  --scopes=https://www.googleapis.com/auth/devstorage.read_only,\
https://www.googleapis.com/auth/logging.write,\
https://www.googleapis.com/auth/monitoring.write,\
https://www.googleapis.com/auth/service.management.readonly,\
https://www.googleapis.com/auth/servicecontrol,\
https://www.googleapis.com/auth/trace.append
gcloud compute instances start lescall-agent --zone asia-south1-c
```

- **If calls break rather than recordings** (the SA swap touches nothing in
  the telephony path, but): `bash phase14_runtime/check_runtime.sh`, then
  `docker compose -f deploy/docker-compose.services.yml restart runtime`.
  FreeSWITCH/Kamailio are on the host and unaffected by the container
  recreate; the static IP survives the stop so the Plivo ACL is unchanged.

NOTE: `lescall-recsync.timer` on the host is still enabled and duplicates the
containerized recsync pass every 15 min. Harmless (uploads are idempotent),
but disable it once the container pass is confirmed working:
`sudo systemctl disable --now lescall-recsync.timer`.

## 7. Verification checklist (plan §6)

- [ ] `python3 -m unittest discover tests` — all green (also under
      `calling-agent/venv/bin/python`).
- [ ] After service restart: place an inbound call — log line
      `Active agent: <slug> (direction=inbound did=8065953160 ...)` and a
      `calls` row appears (`sqlite3 shared/db/lescall.db 'select * from calls'`).
- [ ] Outbound dial from dashboard — bridge log shows the same slug that was
      active at dial time; `call_link.json` entries now carry `agent_slug`.
- [ ] Register a 2nd DID bound to a different slug (dashboard → numbers),
      apply, remove catch-all (step 3), call both DIDs concurrently → two
      sessions log two different agents; two `calls` rows carry two owners.
- [ ] `kamcmd permissions.dumpAddresses` matches the union of provider_ips.
- [ ] Kill test: `mv shared/db/lescall.db{,.bak}` → calls still answered by the
      active_agent.json agent (legacy fallback) → restore.

## 8. Containerized services + minute metering + log shipping (2026-07-14)

### Architecture after cutover
`docker compose -f deploy/docker-compose.services.yml` runs, on the HOST
network (FreeSWITCH/nginx loopback expectations unchanged):
- **lescall-api** — dashboard/server.py on :8080
- **lescall-runtime** — socket_server (:9000, :9004) + ws_audio_receiver
  (:9001) via deploy/runtime_entry.sh; logs in phase14_runtime/logs/docker/
- **lescall-workers** — deploy/workers_entry.sh loop: metering reconcile +
  outbox (5 min), recording sync (15 min), log shipper (60 min)
- **lescall-dashboard** (spa, :8081) — unchanged nginx static container
- **lescall-pg** — SEPARATE compose (docker-compose.postgres.yml); never
  touch it during app deploys.
The repo is bind-mounted at /app: deploying code = `docker compose ...
restart api runtime workers`. Rebuild the image only when requirements
change: `docker build -f deploy/Dockerfile.app -t lescall-app:latest .`

### Cutover / rollback
Cutover (done 2026-07-14): `systemctl stop --now lescall-dashboard
lescall-runtime lescall-recsync.timer && systemctl disable ...` then
`docker compose -f deploy/docker-compose.services.yml up -d`.
ROLLBACK: `docker compose -f deploy/docker-compose.services.yml down`
then `systemctl enable --now lescall-dashboard lescall-runtime
lescall-recsync.timer`. Units are left installed for exactly this.

### Minute metering (BILLING — handle with care)
- `usage_ledger` (one row per call, conn_id == FS UUID) + `usage_monthly`
  rollups. Written by the bridge (open/15s-heartbeat/close), refereed by
  `python -m shared.usage_metering --once` (workers loop): FreeSWITCH CDR
  `billsec` (/var/log/freeswitch/cdr-csv/Master.csv, read via freeswitch
  group gid 994) overrides the pipeline clock; crashes recover from the last
  heartbeat; no-CDR fallback after 24 h. `billsec_final` is the only number
  invoicing may use; manual fixes ONLY via usage_metering.set_manual (note
  required). Cross-check: `python -m shared.usage_metering --report`.
- User-facing: GET /api/usage/minutes, Topbar minutes chip, Usage page.
- BILLING_ROUNDING in deploy/.pg_env (per_call_ceil default).
- BURN-IN: treat numbers as shadow until a week of --report shows no
  unexplained clock_disagreement rows.

### Logging
- Every API request → dashboard/logs/requests.jsonl (rotated 20MB×10).
- Semantic actions → audit_events table (shared/audit.py).
- Hourly shipper → gs://lescall-logs (90-day lifecycle), state in
  shipped_logs table. Config: GCS_LOGS_BUCKET in deploy/.pg_env.

## 9. Sentinel — 24/7 self-healing watchdog (2026-07-14)

`lescall-sentinel.service` (host systemd, NOT in Docker) runs
`shared/sentinel.py` every 30s: checks containers/ports/FreeSWITCH(SIP
OPTIONS probe)/nginx/edge/DB/live-state freshness/workers/inbound-CDR
silence/stuck ledger rows/disk/memory/GCS auth. Auto-repairs by restarting
the owning container or unit — with 10-min cooldown and 3-per-hour cap per
target (then alert-only), and NEVER restarts runtime/FreeSWITCH while a call
is live on :9001. Alerts → sentinel.log + audit_events (owner='sentinel') +
email via sendmail to SENTINEL_ALERT_EMAIL (default tech@dhronai.com,
rate-limited 1/6h per key; exim delivery is best-effort). Status snapshot:
data/sentinel_status.json. Logs: phase14_runtime/logs/sentinel.log.
Verified 2026-07-14: killed SPA container → detected+repaired in 18s.
Tunables (deploy/.pg_env): SENTINEL_INTERVAL, SENTINEL_INBOUND_ALERT_H,
SENTINEL_DISK_ALERT_PCT, SENTINEL_MEM_MIN_MB, SENTINEL_ALERT_EMAIL.

KNOWN LIMIT: carrier-side failures (Plivo not delivering inbound) can only
be DETECTED (inbound-silence alert), not fixed locally — check Plivo console:
account credit, inbound zentrunk 68928093167632317 status, DID origination
URI must be sip:35.207.225.237:5080.

### 9b. Inbound loopback probe (provider-agnostic, 2026-07-14)
The sentinel actively verifies each tenant DID's INBOUND delivery daily at
SENTINEL_PROBE_HOUR_UTC (default 03:xx UTC) — no carrier API needed, works
for any SIP provider: it originates a short parked self-call through the
tenant's OWN trunk to their own DID; the call must come back as a genuine
inbound leg. +OK answer = loop proven; NO_ANSWER = carrier-side failure →
alert naming the owner. Probe legs are killed within seconds and their
usage_ledger rows are marked billable=0 ('sentinel loopback probe').
Manual run: `python -m shared.sentinel --probe`. Disable: SENTINEL_PROBE=off.
Inbound-silence alerts are per-DID (CDR public-context rows by destination).
First live run (2026-07-14): caught +912264231897 (alwynantonyben@) inbound
DEAD (NO_ANSWER via their own trunk) while +918065953160 verified OK.

## 10. Intelligence layer (2026-07-20) — automatic post-call pipeline

Design doc: `deploy/PLAN-intelligence-layer.md`. Every completed call is
analyzed automatically: extraction (gemini-2.5-flash) → embeddings
(gemini-embedding-001, pgvector) → follow-up decision → customer memory →
automation rules → notifications. No manual analysis exists anymore
(`/api/analysis/run` returns 410; the legacy followup-automation auto-dial
scheduler is disabled in server.py startup).

**Processes**
- `shared/intelligence/worker.py` runs as a second, supervised process inside
  lescall-workers (see deploy/workers_entry.sh) — NEVER fold it into the
  5-min billing tick. Liveness: `docker logs lescall-workers | grep 'intel:'`
  and the `settings(owner='', key='intel_worker_beat')` row.
- Queue = `intel_jobs` table (SKIP LOCKED claims, backoff, DLQ status
  'failed'). DLQ surface: `GET /api/intel/jobs?status=failed`, re-drive via
  `POST /api/intel/jobs/{id}/retry`. A sweeper enqueues any call the teardown
  hook missed — coverage is guaranteed, backfills happen automatically.

**Database**
- Versioned migrations: `shared/db/migrations/NNNN_*.sql`, applied MANUALLY:
  `docker exec -w /home/angelo/lescall lescall-workers python -m shared.db.migrate`
  (`--status`, `--dry-run`, `--down NNNN` for the testing env). Never at boot.
- pgvector 0.8.1 was compiled from source INSIDE lescall-pg (survives
  restarts, lost only on container recreation — which is forbidden). Disaster
  recovery: `bash deploy/pg_provision_pgvector.sh` then re-run migrations.
- Tests run against a `lescall_test` database in the same instance
  (`calling-agent/backend/tests/test_intel_*.py`) and rebuild it from scratch
  — that fixture doubles as the DDL disaster-recovery drill.

**LLM / quota**
- Extraction + reports: `gemini-2.5-flash` (INTEL_LLM_MODEL) — a different
  quota bucket from the live-call `gemini-3.1-flash-lite`, keys shared via
  calling-agent/.env; point INTEL_LLM_API_KEY at a dedicated key to isolate
  further. reasoning_effort is set explicitly (thinking eats max_tokens).
- Embeddings: `gemini-embedding-001`, 768-dim, via the same OpenAI-compat
  endpoint's /embeddings.

**In-call commitment clarification** is per-agent opt-in:
`follow_up.clarify_commitments.enabled: true` in agents/<slug>/agent.yaml.
Off by default — existing agents' behavior (and the golden prompt fixtures)
are untouched.

**Deploy**: api/runtime/workers = `docker compose restart <svc>` (bind mount);
SPA = `docker compose build spa && up -d spa`. Migrations before restarts.

## 11. Call-experience latency + continuation merge (2026-07-21)

Greeting/turn latency fixes + pause-tolerant turns. All tunables are env vars
on the **runtime** container (calling-agent/.env or compose environment);
`docker compose -f deploy/docker-compose.services.yml restart runtime` to apply.

**Greeting path** (was ~2.2s ESL→playback; target ≤0.8s warm)
- `PREFLIGHT_CACHE=true` — preflight verdict cached per (slug, sha, revision);
  agent edits self-invalidate via the registry watcher.
- Session routing DB work (dial lookup → slug → owner) now runs in ONE worker
  thread (`TelephonySession.create`), not serially on the event loop.
- `METERING_OPEN_DEFERRED=true` — usage-ledger row opens in background (≤1s
  later); close/reconcile unchanged.
- `TTS_CANNED_CACHE=true`, `TTS_CACHE_DIR=<repo>/data/tts_cache`,
  `TTS_CACHE_MAX_ENTRIES=500` — greeting/fillers/canned lines replay from disk
  PCM: 0ms synth, 0 Sarvam credits after first synthesis per variant.
- `TTS_OUTPUT_SR=8000` — Sarvam synthesizes at 8k; TelephonySink skips the
  blocking ffmpeg downsample entirely. Rollback: `TTS_OUTPUT_SR=16000`.

**Per-turn dead air** (was ~1.25s fixed before STT/LLM even start)
- `HARD_FLUSH_SILENCE_FRAMES=28` (560ms endpoint; old 35=700ms). Tier-b after
  continuation merge is validated: `20` (400ms).
- `TRANSCRIPT_MERGE_SKIP_WHEN_IDLE=true` — the 550ms merge wait now only
  happens when a follow-on segment can actually exist (`TRANSCRIPT_MERGE_WINDOW_MS`).
- `TURN_SILENCE_THRESHOLD_MS=1200` (TurnManager backstop; was 1600).
- `SARVAM_HTTP_SHARED=true`, `SARVAM_KEEPALIVE_EXPIRY_S=300` — one HTTP pool
  per process for STT/TTS (per-call clients made warmup a no-op after call 1).
- `BOOT_WARMUP=true` — ws_audio_receiver warms STT/TTS/LLM at process boot.
- `LANG_LLM_CLASSIFY_FIRST_TURN=false` — first turn never blocks on the
  language-classify LLM round-trip.
- `GREETING_ECHO_EARLY_EXIT_FRAMES=3`, `GREETING_ECHO_TAIL_MS=100` — the
  post-greeting echo gate opens faster for the caller's "hello".
- Grep `[LATENCY] speech_end→engine` for the per-turn fixed-tax number;
  `[ENGINE] Merge window skipped (idle)` should be the common case.

**Continuation merge** (pause-tolerant turns; OFF until canary-validated)
- `ENABLE_CONTINUATION_MERGE=false` → flip `true` to enable. When ON: caller
  pauses mid-utterance → agent answers the partial input fast → if the caller
  resumes within `CONTINUATION_WINDOW_MS=2000` of their own speech end, the
  reply is cut, the router rolls the turn back (memory, slots, stage, audit —
  see app/voice/turn_transaction.py) and re-runs ONE merged utterance.
  Calendar bookings defer until turn commit so a superseded turn never writes.
- Hearing during the window: `CONTINUATION_BARGE_FACTOR=1.3`,
  `CONTINUATION_BARGE_FRAMES=5`, `CONTINUATION_ECHO_EXIT_FRAMES=4`,
  `CONTINUATION_MAX_MERGES=3`. Outside the window all barge-in/echo constants
  are unchanged.
- Grep `[CONT-MERGE]` (merges) and `[TURN-TXN]` (rollbacks/commits).
- Tests: `calling-agent/backend/tests/test_continuation_merge.py`; full suite
  must pass with the flag both unset and set.

## 12. Customer Intelligence Hub — persistent memory in live calls (2026-07-21)

Closes the memory loop: `customer_memory` (written after every call since the
intelligence layer shipped) is now READ — by the extraction LLM and, crucially,
by the live-call runtime, so returning customers are greeted as known.

- **Write side (workers):** pipeline stages are now
  `extract → embed → followup → memory → digest → timeline → automation → notify`.
  `digest` precomputes `customer_memory.context_digest` (≤600 chars) at write
  time; `timeline` appends `contact_events` rows (unified per-contact timeline,
  idempotent via `uq_contact_events_ref`). The sweeper backfilled both across
  all historical calls automatically. Memory itself upgraded from capped lists
  to a governed fact lifecycle: `{text, call_id, date, confidence,
  status: active|superseded|expired, updated_at}` — dedup-by-meaning
  (similarity ≥0.60 updates in place), slot conflicts supersede (flip, never
  delete), per-category TTL expiry (promises 45d, objections 240d, …), plus
  `preferred_language` (from calls.language) and `preferred_call_time`
  (call-start-hour histogram, IST buckets).
- **Failed dials:** the intel worker's sweeper runs a CDR read-alongside pass
  (`shared/intelligence/dial_outcomes.py`, every 5 min) that joins unanswered
  CDR legs to `call_links` dials and lands classified `call.missed` events
  (busy|no_answer|rejected|invalid_number|network_failure) with retry hints.
  Billing/`usage_ledger` untouched.
- **Read side (runtime):** `_resolve_call_routing` (already off-loop) adds one
  contacts lookup + one customer_memory read (~1ms, 5-min in-proc TTL cache)
  and hands the digest to `ConversationRouter.set_customer_context()` → the
  `━━ KNOWN CUSTOMER ━━` prompt band + `{contact_name}` greeting
  personalization + no-re-ask name prefill. Fetch failure → None → call
  proceeds exactly as before. Grep `[BRIDGE] customer context` for fetch
  timing and `[CONTEXT] known customer` for injection.
- **Flags:** env `INTEL_PRECALL_CONTEXT=false` kills the fetch platform-wide;
  per-agent `memory: {use_customer_context: false}` in agent.yaml opts one
  agent out (default ON). `INTEL_DIAL_OUTCOMES_ENABLED=false` stops the CDR
  outcome pass.
- **Hub API/UI:** `GET /api/intel/contacts/{id}` (+`/timeline`, `/calls`),
  owner-scoped with `contact_grants` sharing (read|edit, owner-only to grant —
  flattened-roles doctrine, no admin). `PATCH /api/intel/contacts/{id}` sets
  `contacts.primary_agent_slug` (bridge defaults it to the first agent that
  talked to the contact). SPA: `/contacts` rows click through to
  `/contacts/:id` (ContactHub.tsx) — timeline, AI profile, score trend,
  memory, follow-ups, promises, lifetime stats. SPA is baked: UI changes need
  `docker compose -f deploy/docker-compose.services.yml up -d --build spa`.
- **Migrations:** `0006_contact_hub` (+`.down.sql`) — `context_digest`/
  `digest_updated_at` on customer_memory, `contact_events`, `contact_grants`,
  contact indexes on followups/commitments. `contacts.primary_agent_slug` is a
  boot MIGRATIONS ALTER (core table). Never edit 0006 once applied.
- **Tests:** `test_intel_memory.py`, `test_intel_timeline.py`,
  `test_precall_context.py`, `test_contact_hub_api.py` + extended
  `test_intel_stages.py` (8-stage e2e). Full suite: 334 passing.

## 13. Call-repair platform rules — hello-replay + dead-air reprompt (2026-07-21)

Two universal behaviors for EVERY agent (no YAML needed), both in the live
engine:

- **Early-call "hello?" replay** (`conversation.py`): a bare hello — any
  supported script (hello/हेलो/હેલો), optionally repeated or with filler
  ("are hello haan") — within `CALL_REPAIR_HELLO_WINDOW_S` (default 25s) of
  the greeting means the caller didn't hear us. The agent replays its last
  utterance verbatim instead of chatting. Capped at 2 replays per call so a
  deliberate "hello" opener still gets a conversation. Grep `[REPAIR] Early
  'hello'`. Gotcha encoded in the tokenizer: Python's `\W` shreds Indic
  matras — the hello matcher keeps the Devanagari/Gujarati blocks whole.
- **Dead-air reprompt** (`pipeline.py:_silence_watchdog`): agent finished
  speaking, then NO caller voice activity for `SILENCE_REPROMPT_S` (default
  6s) with nothing in flight anywhere (no STT segments, no queued
  transcripts/TTS, no LLM generation, playback fully drained, no farewell/
  outcome decided) → the agent says "Sorry, I couldn't hear you." (localized
  en/hi/gu) + repeats the last question. If the caller has not spoken at all
  yet (turn 0), it replays the utterance plain — no "couldn't hear YOU"
  accusation. Max `SILENCE_REPROMPT_MAX` (default 2) consecutive reprompts;
  the cycle resets on any real caller turn. The reprompt is recorded in
  conversation memory but never overwrites last-response memory, so repeats
  never compound. Defaults ON in telephony, OFF for CLI mic
  (`SILENCE_REPROMPT_ENABLED` overrides either way). Grep `[REPAIR] Dead air`.
- Interplay: the older STT-empty fallback (speech detected but empty
  transcript, feature 2/2026-05-30) and this watchdog are mutually exclusive
  by construction — one requires detected speech, the other requires none.
- Tests: `tests/test_call_repair.py` (14) — replay/cap/window/scripts,
  reprompt composition per language, and the watchdog decision matrix.

## 14. Follow-up execution modes — auto-dialling ON by default (2026-07-27)

The 0007 auto-dialer dialled EVERY decision='yes' follow-up (it placed its
first 12 real calls the moment the Thu 10:00 IST window opened). Migration
0008 made auto-dialling opt-in — and the opt-in was never exercised: 225
follow-ups accumulated with 0 `mode='auto'` rows, so the executor ran ~720
empty sweeps/day and every "call me tomorrow" silently died. On 2026-07-27
the business made follow-up calling a **platform universal rule**: ON for
every agent, existing and future.

- `followups.mode` `manual` | `auto` — only `auto` rows are dial candidates
  (`autodial._DUE_SQL`). Per-row toggle in FollowUps → Edit (re-arming
  clears a previous permanent block); also settable at create.
- **Default is now auto**: `_agent_auto_execute` (shared/intelligence/
  followup.py) returns True when the agent YAML says nothing — missing
  agent dir/file/key all mean True. An agent opts OUT with
  `follow_up.auto_execute: {enabled: false}`. The schema-side default
  (agent_definition/schema.py from_dict) mirrors this — change both
  together. `INTEL_AUTODIAL_ENABLED` stays the platform kill switch above
  everything.
- **Relative callbacks** ("call me in 10 minutes"): captured in-call by the
  clarifier (`_parse_relative_offset`, commitments carry `offset_min`),
  extracted post-call (`follow_up.suggested_offset_minutes` in the LLM
  schema), scheduled from CALL END. Customer-stated times
  (`auto_scheduled=0`) skip the 5-min grace and use the courtesy window
  (`INTEL_AUTODIAL_CB_HOUR_START/END`, default 07-21 IST, any weekday)
  instead of the business window; AI-picked times keep grace + 10-19
  Mon-Sat. Poll is 60s (`INTEL_AUTODIAL_POLL_S`).
- **Opening line**: the executor stamps `call_links.callback_meta`
  ({reason, topic=customer_intent, requested_when, source_call_at}); the
  bridge feeds it to `set_callback_context(meta=...)` and
  `_callback_opening()` renders "we spoke earlier today about X and you'd
  asked me to call you back — is now a good time?" in the agent's language
  (en/hi/gu templates, no LLM on the latency-critical opening).
- New guardrails: `superseded` (a newer call to the contact after the
  follow-up was created — permanent), per-row `max_attempts` (NULL =
  platform default 3), explicit `next_attempt_at` linear backoff
  (gap × attempt#). A dangling contact no longer blocks: the dial phone
  falls back to the originating call's number (the 38-blocked-rows
  incident).
- Memory on the call: the executor writes the follow-up reason + chased
  commitment + prior-call summary into `call_links.context_note`
  (→ WHY-YOU-ARE-CALLING-BACK prompt band); the KNOWN CUSTOMER band already
  carries the customer digest. `call_links.followup_id` links the dial.
- Outcome loop: originate result → `followups.execution_outcome`
  (provisional); when the placed call is ANALYZED with a real caller turn,
  `stage_followup`'s completion hook marks the follow-up completed with
  `executed_call_id` (voicemail nuance documented in code). `sweep_overdue`
  no longer flips in-retry auto rows to 'missed'.
- Tests: `test_autodial_guardrails.py` (57) — mode gate, superseded,
  per-row caps, backoff, phone-fallback.
- Open product items (deliberately NOT built): in-call "stop calling me" →
  DND disposition; TRAI NDNC scrubbing; billing stamp for platform-placed
  calls.

## 15. Agent-initiated hangup + close-permission rule (2026-07-27)

Before this, no engine-initiated hangup existed — the closing line played
and the channel stayed up until the caller hung up (dead air, burned
minutes). Platform universal rule, every agent:

- **Supreme rule — permission to close**: the prompt band
  `━━ ENDING THE CALL ━━` (prompt_blocks.call_closure_block, rendered
  unconditionally in every system prompt) makes the agent ask "anything
  else I can help you with?" before any goodbye. Deterministic backstop:
  `_is_close_confirmation` (agent's last response asked the permission
  question + caller replies a short "no / nahi bas / that's all") routes
  into the farewell close. `_contains_farewell` additionally catches
  goodbyes the intent classifier scores as affirmation ("okay bye").
- **hangup_pending** (ConversationRouter) arms ONLY on caller-consented
  or terminal closes: farewell exchange, close-permission confirm,
  **confirmed callback commitment** ("call me in 10 minutes" → confirm +
  goodbye tail → close; the request to be called later IS consent to end
  now — `_arm_callback_close`), wrong number, DND / decline threshold,
  LLM-failure graceful close. It is NOT `state.is_closing` (which also
  fires on booking-confirmed while the conversation continues). Any new
  caller utterance clears it — "wait, one more question" after the
  goodbye is answered, not cut off.
- **Busy brush-offs** ("sorry not free right now / abhi busy / atyare
  nahi" — `_BUSY_NOW_RE`) route into the callback pin-down flow instead
  of the objection strike counter: one polite "about how many minutes
  later should I call you back?" question, then confirm + close on the
  answer. No decline strike is burned.
- **Prompt text can NEVER hang up a call** — only the engine paths above
  do. The Copilot doctrine (agent_copilot_api.py) forbids the builder
  from writing guardrails that claim the agent will "end the call"; a
  real user incident (2026-07-27) had the Copilot ship exactly that
  rule, which also fought the pin-down ("do NOT ask for a better time").
  **Superseded 2026-07-29 (§17)**: termination requests are now REAL
  engine wiring via `call_flow.termination` — the doctrine directs the
  Copilot to write them there instead of refusing.
- **Execution**: `TelephonySession._playback_loop` polls (0.5s get
  timeout) → `_maybe_agent_hangup` waits for wav_queue empty +
  `playback_busy_until` + grace, min 3s after arming (covers TTS synth
  of the farewell) → `hangup_request` over TCP 9004 → socket_server
  `_dispatch_hangup_request` queues an **event-locked** `hangup
  NORMAL_CLEARING` app, so FreeSWITCH ends the channel only after queued
  farewell audio finishes. Normal teardown (recording, metering, intel
  enqueue) then runs off the WS close as always.
- Kill switch: `ENABLE_AGENT_HANGUP=false` (bridge env). Tuning:
  `AGENT_HANGUP_MIN_WAIT_S` (3.0), `AGENT_HANGUP_GRACE_S` (1.0).
- Tests: `test_agent_hangup.py` (arming/cancel semantics),
  `test_autodial_guardrails.py` (customer-stated time grace/window),
  `test_commitment_clarify.py` (relative offsets, default-on).

## 16. Follow-up calls require human approval (2026-07-29) — INCIDENT FIX

**What happened.** At 05:36 UTC the auto-dialer called a real customer
(+91 87678 65050, contact 88) on follow-up 216 → call 304, `placed_by=autodial`.
The source was a 44-second call two days earlier on which **nobody promised
anything** — the pipeline recorded it as `promised_by='agent_promised'`,
`confidence 0.9`, and the dialer trusted that. Of the 20 rows armed that
morning, 15 had no commitment on the source call at all, and several were calls
that had failed on *our* side ("call failed due to technical audio issues")
logged as agent promises with confidence 1.0.

**This was the second occurrence.** Migration 0008 made auto-dialling opt-in on
2026-07-23 after the 0007 executor placed 12 unwanted calls; commit `b693a8c`
(§14) then flipped the per-agent `auto_execute` default back ON and reopened it.
The lesson: `mode='auto'` is a *preference* and a code default can synthesise
it. Approval is a *fact about a person's action* and nothing but the approve
endpoint can write it.

### The gate — three independent layers, all must pass

1. **SQL** — `shared/intelligence/autodial.py` `_DUE_SQL`:
   `AND f.approval_state = 'approved'` +
   `AND (f.approval_expires_at IS NULL OR f.approval_expires_at > now())`.
2. **Pure guardrail** — `blocked_reason()` returns `not_approved` /
   `approval_expired`, evaluated second (only DND outranks it). Absence reads
   as unapproved: a missing key never means "go ahead". Neither code is in
   `PERMANENT` — an unapproved row must stay rescuable by the human it waits on.
3. **Dial site** — `dashboard/server.py` `_autodial_once()`, immediately above
   `_record_dial`: refuses and `logger.error`s if it ever sees an unapproved
   row. If that line fires, layers 1–2 have regressed; treat it as a bug.

Approval cannot be manufactured elsewhere: `approval_state` is **not** in
`_FU_PATCHABLE` (so `PATCH /followups/{id}` cannot grant it), and neither the
pipeline, manual create, nor automation rules set it — everything defaults to
`pending` via migration 0011.

### Schema (0011)

`followups.approval_state` (`pending|approved|declined`, NOT NULL DEFAULT
`pending`), `approved_at`, `approved_by`, `approval_expires_at`, `declined_at`,
`declined_by`. `idx_followups_autodial` rebuilt with the approval predicate;
new `idx_followups_review` for the queue. **No backfill** — the DEFAULT put all
264 existing rows in `pending`, which is exactly the intended blast radius.

### API (`dashboard/intel_api.py`, all owner-scoped, all audited)

- `GET  /api/intel/followups/review-queue?include_declined=0|1&limit=`
  → `{items[], total (pending only), shown, approval_ttl_hours, max_attempts_per_approval}`
- `POST /api/intel/followups/{id}/approve` `{mode?, expires_in_h?}`
- `POST /api/intel/followups/approve` `{ids[], mode?, expires_in_h?}` (bulk)
- `POST /api/intel/followups/{id}/decline` `{note?}`

Approval TTL is **72 h** (`_APPROVAL_TTL_H`, matched to `AUTODIAL_MAX_AGE_H`).
`_approve_rows` refuses DND contacts regardless of what the UI sent, and
deliberately does **not** clear `autodial_blocked_at` — approving the *content*
of a call is not re-arming a permanently blocked row.

### Evidence — why the reviewer is shown words, not conclusions

`_evidence_for()` joins `commitments` **on `call_id`, not
`followups.commitment_id`** (that FK is NULL on essentially every row, while
all 89 commitments carry a machine-verified verbatim `quote`). Two tiers:
`strength='quoted'` (≥1 commitment → shows the quote and who said it) and
`strength='inferred'` (none → amber block stating *"No promise was recorded on
this call"*, and when `promised_by=='agent_promised'`, *"— despite labelling it
an agent promise"*). Plus `right(transcript, 4000)` → last 8 turns as context.
**The UI contradicts the AI's claim rather than restating it.** That is the
point of the screen; do not "simplify" it back into a summary.

### UI

`frontend/src/components/FollowupReview.tsx` — `FullscreenModal` opened from
"Review & approve calls" in the Follow-ups header (plus an amber banner while
anything is pending). Per-row Approve / Don't call, bulk select-all (skips
declined and DND), "Show declined" toggle so a decline is never a one-way door.
Row badges: `auto-call approved` only when `mode==='auto' && approval_state==='approved'`;
`needs approval` otherwise. Manual "Call now" stamps a `mode='manual'` approval
before dialling, so every call — hand-placed or automatic — has a named
authoriser in `followups.audit`.

### Tests

- `test_autodial_guardrails.py` (75) — pure-function gate, incl. fail-safe on a
  missing key and a source assertion that the predicate is still in `_DUE_SQL`.
- `test_followup_approval.py` (12) — **PG-backed**, calls `due_candidates()` for
  real. This is the layer a pure-function test cannot reach and the one that
  would have caught the incident.

### Operating it

- Kill switch: `INTEL_AUTODIAL_ENABLED` in `deploy/.pg_env`. Set to `false` on
  2026-07-29 as the incident stop, and **back to `true` the same day once the
  gate below was live** (log line: `follow-up auto-dialer ON`; all 266 rows sat
  at `pending`, `due_candidates()` returned 0 with the dialer running). It is a
  kill switch, not the consent mechanism — off stops everything, on grants
  nothing. Change it with
  `docker compose -f deploy/docker-compose.services.yml up -d api`
  (**a bare `restart` does not re-read the env file**; that bit the cont-merge
  canary on 07-24). Verify with `docker exec lescall-api env | grep AUTODIAL`.
- Resting-state check — this should return only rows you intend to be called:
  `SELECT id, approved_by, approval_expires_at FROM followups
    WHERE approval_state='approved' AND approval_expires_at > now();`
- One approval permits up to `INTEL_AUTODIAL_MAX_ATTEMPTS` (3) attempts over
  the TTL; the approve dialog states this. Set the env to 1 if the policy is
  literally one call per approval.

### Also removed

The dormant legacy `_automation_scheduler_loop` (`followup_automations`) is
**deleted**, not commented out. It dialled real phones for any past-due
'scheduled' row with no approval/DND/window/cap, read a table the approval gate
never queries, and sat one uncommented line from live. `_dial_automation_entry`
survives for the operator-triggered `call-now` endpoint.

### Campaign engine — same limits, applied 2026-07-29

The bulk dialler was the other path that could ring a list of people, and it
had none of the above. All in `dashboard/server.py`, pinned by
`test_campaign_guardrails.py` (21):

- **DND.** `disposition='rejected'` — how an in-call "stop calling me" is
  recorded (`test_dnd.py`) — is dropped from the audience *before* the cap, so
  a list of opted-out contacts can't eat the budget. Re-read from the DB before
  each dial too (`_contact_is_dnd`, **fails closed**): someone who opts out on
  call 3 is not dialled on call 40 of the same run.
- **Caps.** `CAMPAIGN_MAX_CALLS` (200) and `CAMPAIGN_MIN_DELAY_SECONDS` (3),
  both env-overridable. `max_calls:0` now means "up to the ceiling", not
  "unlimited"; a larger request is clamped. Ceilings ride along on
  `/campaign/status` so the form states the real number.
- **Calling hours.** Refuses to start outside `autodial.within_window()` and
  **halts mid-run** when the window closes, stamping `camp["error"]` with how
  many were left undialled.
- **Honest refusals.** An empty or all-rejected audience now returns **400**;
  it used to return `ok:false` with HTTP 200, which the SPA toasted as
  "Campaign started" while nothing had been queued.
- **Audience fix.** `to_contact_later` is a *disposition*; the runner matched
  only `call_status`, so that audience silently resolved to zero.
  `_campaign_audience` handles both columns.
- **UI.** Start is behind a confirm naming the exact number of people; tallies
  gained a "Do not call N" badge.

### Known, NOT fixed by this change (needs a decision)

- **`POST /api/outbound/dial/{handle}`** does not check DND; `?force=1` removes
  the duplicate-dial guard. It is one phone per click by a logged-in human, so
  it was left alone deliberately — but a mis-click on a rejected contact still
  connects.
- **Sentinel loopback probe** (`shared/sentinel.py`, 03:00 UTC daily) self-calls
  our own DIDs from `phone_numbers` — it does **not** ring customers today. If a
  BYON tenant ever registers a number that terminates on a human handset it
  would, so make it per-number opt-in before that happens.

## 17. Call-termination policy — configurable, Copilot-controlled (2026-07-29)

**Why.** Live calls were effectively only ending when the caller said a
literal "bye" (§15's consented paths). Everything else — the LLM's own
judgment that the conversation was over, callers who walked away mid-call,
business rules like "end once the booking is confirmed" — had no path to a
hangup, and the Copilot doctrine explicitly refused such requests. Fixed by
making termination a per-agent config the engine executes.

**Config: `call_flow.termination`** (agent YAML; all keys optional, `{}` =
platform defaults; Copilot writes this from user requirements):

- `llm_end_call` (default **true**): the `━━ ENDING THE CALL ━━` prompt band
  teaches the model to append the token `[END_CALL]` after its goodbye line.
  The engine strips the token BEFORE `_validate_response` (whose markdown
  filter would leave spoken "ENDCALL" litter) in `_call_llm_stream`, then
  `_consume_llm_with_filler` arms `hangup_pending` exactly like the
  deterministic farewell paths. Caller speech still cancels; §15's drain
  logic unchanged. Marker regex: `_END_CALL_RE` (bracketed any-case, bare
  form upper-case only so prose "end call" survives).
- `end_conditions: [str]`: plain-English business rules rendered every turn
  as `━━ END-CALL RULES ━━`; they take precedence over the permission-to-
  close question and act through `[END_CALL]`.
- `farewell_patterns: [str]`: extra caller phrases (any language/script)
  that close deterministically via `_contains_farewell` — which also gained
  built-in unambiguous hangup phrases ("phone rakhta hoon", "મૂકું છું",
  "gotta go"...). `_DONE_REPLY_RE` now accepts bare thanks
  ("thank you/dhanyavaad/aabhar") as the close-permission confirm.
- `silence_hangup: {enabled, grace_s}` (default enabled, grace 10s): after
  both §13 dead-air reprompts go unanswered plus the grace window,
  `generate_silence_goodbye()` plays one goodbye, stamps
  `call_outcome=no_response` + `extra_flags.silence_hangup`, arms
  `hangup_pending`. The caller speaking re-opens the call and re-arms the
  full repair cycle. Env: `SILENCE_HANGUP_ENABLED` (telephony-default on),
  `SILENCE_HANGUP_GRACE_S`.
- `max_call_minutes` (0/absent = no cap): watchdog fires at the first quiet
  moment past the cap — `generate_timeout_goodbye()` plays a wrap-up line
  and arms a **hard** hangup (`_hangup_hard`), the ONE close caller speech
  does not cancel. `call_outcome=max_duration`.

**Where.** Engine: `conversation.py` (marker strip/arm, farewell vocab,
goodbye generators, hard-close gate at top of `process_stream`). Watchdog:
`pipeline.py` `_silence_watchdog` (now also max-duration + silence-hangup;
decisions in `_silence_hangup_due` / `_max_duration_due`, shared
`_pipeline_quiet`). Prompt: `prompt_blocks.call_closure_block`. Schema:
`CallFlowConfig.termination`. Copilot: schema docs + rewritten hanging-up
doctrine + `_normalize_definition` shape guards (drops <4-char farewell
patterns — "ok" would cut live calls) + two `_quality_warnings` lints
(end_conditions with llm_end_call=false; cap under 2 minutes).

**Tests.** `test_call_termination.py` (30: vocab, marker, silence/max-dur,
watchdog decisions, prompt rendering), `test_copilot_quality_lints.py`
(termination lints + normalization + doctrine pins), golden prompts
regenerated (closure band now carries the token doctrine).

**Rollback.** Per-agent: `termination: {llm_end_call: false}` (kills the
LLM signal), `silence_hangup: {enabled: false}`. Platform:
`SILENCE_HANGUP_ENABLED=false`; §15's `ENABLE_AGENT_HANGUP=false` still
kills ALL agent-initiated hangups at the bridge.

## 18. Connect hello-loop counters — platform-wide (2026-07-30)

**Problem (measured, 2026-07-29 logs).** Calls looped at connect: caller
says "hello" every ~2s, agent stays quiet. Root causes: bare `Hello`
openings (3 agents) invited the ping-pong; median speech-end→first-audio
was 2.47s (p90 3.26s) ≥ the human hello timeout; each next hello BARGED IN
and killed the reply before its first WAV was flushed (socket_server showed
zero PLAYBACK_SEND for the killed replies); Sarvam misdetected bare hellos
as Odia/Kannada forcing a retry (+350ms, 33×/day); a 240ms hello fell under
the 300ms STT floor; merged `hello hello hello` chains filled `name` and
burned stage max_turns. Full diagnosis: `deploy/PLAN-connect-hello-loop.md`.

**Fixes (one shared engine — every agent, current and future).**

| Layer | Behaviour | Where |
|---|---|---|
| Router | Instant identity intro on the first early bare "hello" when the last utterance was itself bare — no LLM turn. Composed from identity when the config opening is bare/placeholder (`connect_intro_line`). | `conversation.py` fast path after the hello-replay branch |
| Router | Dead-air reprompt never parrots a bare "Hello" — substitutes the intro. | `generate_silence_reprompt` |
| Router | Bare hellos never answer QA fields, never gap-fill slots (`_scrub_hello_values`), never burn `discovery_turns`. | `conversation.py` |
| Pipeline | **Delivery guard**: barge-in against a reply with <1s audible (or nothing flushed) is DEFERRED; the STT verdict kills playback only if the interruption wasn't a bare hello. Greeting/replays/intro/watchdog lines are `Response.protected` → guarded at any call age. | `_connect_barge_defer` + deferred verdict in `_conversation_layer` |
| Pipeline | Bare hellos excluded from merge-window/drain merges, CONT-MERGE rollbacks, and accumulator prepends (no more `hello hello hello…`). | `_conversation_layer` |
| STT | Connect window: short segments (<800ms) force the agent's default language instead of `unknown` (kills the Odia/Kannada retry tax); STT floor lowered to 200ms. | `_stt_worker`, `_min_speech_ms` |
| STT | Empty-streak damper: only the SECOND consecutive empty speaks "couldn't hear you" (was: every one of 4 — fed an echo→empty→sorry loop). | `_empty_fallback_speaks` |
| Copilot | OPENING doctrine (see correction below) + `call_flow.connect` schema + normalization; `_enrich_new_agent` fills only a MISSING opening, with a short `Hello!`. | `agent_copilot_api.py` |

**Env knobs (all default ON; delivery guard telephony-only by default).**
`CONNECT_HELLO_FASTPATH` (true) · `CONNECT_DELIVERY_GUARD` (true if
TELEPHONY_MODE) · `CONNECT_WINDOW_S` (12) · `CONNECT_MIN_PLAYED_S` (1.0) ·
`CONNECT_MIN_SPEECH_MS` (200) · `CONNECT_FORCE_LANG` (true) ·
`CONNECT_SHORT_SEG_MS` (800) · `EMPTY_STREAK_DAMPER` (true).
Per-agent: `call_flow.connect: {hello_fastpath: bool, delivery_guard: bool}`
(defaults ON; the block exists only to opt out).

**Rollback.** Per-agent: `call_flow.connect: {hello_fastpath: false,
delivery_guard: false}`. Platform: set the matching env to `false` in
compose (remember: compose *restart* does not re-read env — `up -d`).
All other guards (merge/QA/slot/reprompt hygiene) are unconditional
correctness fixes with no knob.

**Tests.** `tests/test_connect_hello.py` (29) + opening/enricher/connect
tests in `test_copilot_quality_lints.py`. Watchdog/guard decisions are pure
functions tested via SimpleNamespace with bound methods (same pattern as
§17). Golden prompts untouched (no prompt-band changes).

**Doctrine correction (2026-07-30, same day).** The first cut of this fix
also rewrote the 3 bare `Hello` openings into full identity monologues and
made the Copilot lint/repair bare openings. That was WRONG and is reverted:
the owner's intended pattern is **short "Hello" → WAIT → the engine speaks
the identity intro when the caller responds** (the fast-path row above; the
intro is a shortcut so it rides the TTS disk cache — no LLM, ~0ms synth
after first play). Openings are back to `Hello`; the bare-opening lint and
create-time identity-opening composition are deleted; the system-prompt
doctrine now says both styles are valid and the Copilot must NEVER rewrite
a user's opening unless asked (`test_enrich_never_touches_short_opening`
pins this). For short-opening agents, keep `identity.agent_name` /
`company_name` filled — the connect intro is composed from them.

**Open item (5.2 audit).** In call b217ca93 (2026-07-29 11:04) echo
segments born INSIDE the 3.2s ECHO-MUTE window still reached STT — the
echo-gate early-exit (5 frames ≈ 100ms) is the suspect. The damper contains
the blast radius; tuning `ECHO_EARLY_EXIT_FRAMES` needs its own canary.

## 19. TTS accent consistency — platform-wide (2026-07-30)

**Problem (measured, Jul 25-30 logs).** English replies spoken with a heavy
Hindi accent; accent flips mid-call. 87/88 calls opened with hi-IN audio
(state.language was hardcoded "hi" — the agent's `languages.default` only
picked the voice, never the language); the TTS `target_language_code`
followed conversation state, not the text (28 clearly-English lines sent
hi-IN in 5 days, +8% Hindi pace factor riding along); Sarvam's per-segment
STT detection rewrote state unguarded BEFORE transcript validation (21
mid-call accent switches on Jul 29 alone; a Gujarati carrier hold message
flipped an en agent to gu-IN); wrong-accent PCM persisted in the canned
cache ('Hello' replayed 62× from hi-IN audio). Full diagnosis:
`deploy/PLAN-tts-accent-consistency.md`.

**Fixes (one shared engine — every agent, current and future).**

| Layer | Behaviour | Where |
|---|---|---|
| Session | `state.language` seeded from the agent's `languages.default` (clamped to en/hi/gu; exotic codes keep the legacy hi seed). | `ConversationRouter.__init__`, bridge, `run.py`, CLI fallback |
| TTS | `target_language_code` derived from the TEXT being spoken (`tts_language_for`: script wins; code-mixed Devanagari stays hi-IN; Latin follows state only with romanized hi/gu markers). Resolver precedence: Response.language stamp > text > state. | `conversation.py`, `pipeline.py _resolve_tts_language` |
| TTS | LLM sentences stamp their language at ENQUEUE (a mid-queue state flip once spoke an English reply as hi-IN); fillers stamp the state language they were picked by; STT fallback lines stamp their chosen language. | `Response.language` |
| STT | Language mirror guarded: applied only after the transcript survives validation AND the claim matches the transcript's script/markers (bare "hello" and lone "okay" can never flip; en switch needs ≥2 words — same bar as the conversation-side threshold). | `stt_mirror_consistent`, `_stt_worker` deferred write |
| Pace | Follows the same corrected language — English no longer gets the hi ×1.08 factor. | free via the resolver |
| Copilot | New agents get the Sarvam-recommended speaker for their default language (en→ishita/ratan; the old hardcoded simran is below tier for English); missing per-language voice entries inherit the agent's existing speaker (one voice per agent). Existing agents untouched. | `voice_catalog.RECOMMENDED_SPEAKERS`, `_default_definition`, `_enforce_invariants` |
| Logs | One audit line per utterance: `[TTS] lang=<code> src=stamp|text|state speaker=<s>`; `Cache HIT` now records language + speaker. | `pipeline.py`, `tts_sarvam.py` |

**Env knobs.** `TTS_LANG_FROM_TEXT` (true) · `LANG_MIRROR_GUARDED` (true).
Per-agent opt-out: `languages.tts_lang_from_text: false`.

**Cache purge (one-time, at deploy).** `data/tts_cache/` held 13 pure-English
entries keyed hi-IN (created while state was mislabeled). Purge after the
runtime restart: `rm -f data/tts_cache/*.pcm` — self-rebuilding, one synth
per canned line on first play.

**Rollback.** `TTS_LANG_FROM_TEXT=false` → accent follows state again;
`LANG_MIRROR_GUARDED=false` → legacy unguarded immediate mirror; P0 seed is
a constructor arg (one-line revert per call site). Compose env changes need
`up -d` — restart alone does not re-read env.

**Tests.** `tests/test_tts_accent.py` (21) + 4 voice tests in
`test_copilot_quality_lints.py`. Not changed: LLM prompting, response
selection, stage flow, barge-in, cache-key formula, existing agent YAMLs.

**Deliberately out of scope.** IVR/hold-announcement text still counts as a
caller turn for the conversation's own (guarded) language path; stage-goal
fragments spliced into callback greetings (separate bug, recorded in the
plan); per-chunk long-sentence pace damping variation.

## 20. Language fidelity + deterministic contact capture — platform-wide (2026-07-30)

Two live-call failures on `918076562864_20260730_120613` (full diagnosis:
`deploy/PLAN-language-fidelity-and-contact-capture.md`): the agent answered a
pure-Hindi caller in **Gujarati** (twice, back-to-back), and the caller's
email was stored as the LLM's guess `bell@gmail.com`, which the caller's
correction could never overwrite. Everything below is engine-level — it
applies to every existing agent and every future Copilot agent.

### Part A — language fidelity

| Layer | Fix |
|---|---|
| A1 marker regex | `_STRONG_GU_DEVA_RE`/weak tier use Devanagari-block boundaries `(?<![ऀ-ॿ])` — Python's `\w` excludes matras/virama, so "अच्छे" matched marker "छे" mid-word and flipped the call. Weak tier is no longer a substring scan. |
| A2 evidence switch | One per-word vote scorer for all pairs (en/hi/gu): instant switch at density ≥0.6 with ≥2 votes (en additionally needs a function word); single-vote/0.3–0.6 evidence → one-turn confirmation band; acks, names, digits, emails are neutral. Replaces the single-marker fast path and the 2-ASCII-word English switch. 1-turn oscillation guard. Explicit requests stay instant, all pairs. |
| A3 neutral turns | Whisper/script-retry-recovered transcripts never vote (`Transcript.stt_source`), capture/name-pending turns never vote, and the STT mirror's en claim now needs an English function word ("alvin anthony" can no longer flip a call). Mirror skipped entirely for recovered sources. |
| A4 STT pinning | After 2 stable turns the streaming WS reconnects (turn boundary only) pinned to the conversation language — kills the 29×/day pa/bn/kn/ml script roulette. Unpins the moment state moves off it. Mixed-script finalizes salvage clean tokens before any retry ('ਇਹ। K L W Y N' keeps the letters). The forced-language script-retry now actually re-uploads audio via the HTTP rescue client (under streaming it silently retried nothing). Whisper gets the conversation language as a hint. |
| A5 delivery epoch | Every supersede (cont-merge rollback, barge-in drain, deferred kill) bumps an epoch; the TTS player re-checks it between dequeue and synthesis — the double-spoken pre-/post-merge replies are structurally dead. Replays (`Response.replay`) are dropped when a newer response already played. |
| A6 telemetry | `grep RESPONSE-MISMATCH` finds every reply whose text language differs from the directed language. Warning-only. |

### Part B — deterministic contact capture

The LLM asks; the engine captures. `app/voice/contact_capture.py` owns the
spoken-form normalizer (at/dot/एट/डॉट operators, spelled-letter runs incl.
Devanagari letter names "एल दब्यू वाय एल", the ITN `www.` artifact, provider
snap gmial→gmail, TLD fixes). Email-typed slots (engine infers the type for
slots NAMED email even when authored `type: text` — the live LesCall agent):

- **Only writers**: the deterministic normalizer (typed extraction + capture
  turns). `merge_state` rejects `source="llm"` for email/phone slots and
  rejects validator-failing emails from ANY source.
- **Pending until a bare yes**: policy `confirm`, spelled read-back
  ("b. e. n. — at — gmail dot com"), no 2-turn auto-accept, and "okay tell
  me more" does NOT confirm (bare ≤3-word affirmations only). Corrections
  re-enter read-back with the new value.
- **Capture mode** while the email is being asked/confirmed: merge window
  stretches to `CAPTURE_MERGE_WINDOW_MS` (2000ms, up to 8 fragments), STT
  forced en-IN, barge-in threshold ×2, partial spellings buffer silently
  (agent stays quiet while the caller spells).
- **Cap → unresolved, never a guess**: after `max_asks` (2) failed
  read-backs the slot is skipped, `extra_flags.email_unresolved=true` rides
  collected_data into the intel pipeline for human follow-up.
- Copilot provisions new agents' email slots explicitly (type email +
  confirm + spelled + 2 asks) and teaches: ask ONCE, never "speak slowly".
- Phone slots keep their authored merge policy (live outbound flows must
  not stall); they gain digit-groups read-back and capture-mode endpointing.

### Knobs (env unless noted)

`LANG_EVIDENCE_SWITCH` (true) / per-agent `languages.evidence_switch`;
`STT_LANG_PIN` (true), `STT_LANG_PIN_TURNS` (2); `CAPTURE_MODE` (true),
`CAPTURE_MERGE_WINDOW_MS` (2000); accent-era knobs unchanged
(`TTS_LANG_FROM_TEXT`, `LANG_MIRROR_GUARDED`). Compose env changes need
`up -d`, not restart.

### Verification

Tests: `tests/test_language_fidelity.py` (26) + `tests/test_contact_capture.py`
(31); full suite green. On a live call grep for: `[LANG] Evidence switch`,
`[LANG] Turn doesn't vote`, `[STT] Language pinned`, `[STT] Script salvage`,
`[TTS] Dropped stale response`, `[CAPTURE] armed/candidate/confirmed`,
`RESPONSE-MISMATCH`.

---

## §21 — Call hearing + capture hardening, platform-wide (2026-07-31)

Follow-up to §20, driven by call `918076562864_20260731_052423` (Hindi, maya
outbound): the §20 language layer held, but streaming-STT segment accounting
silently destroyed real caller speech, and the email capture armed five turns
after the ask, fought the LLM for ownership, then discarded the confirmed
address at call end (DB got `email: null`, `name: "Panj Loa"` — a spelling
fragment). Full diagnosis + fix spec:
`deploy/PLAN-call-hearing-and-capture-hardening.md`. All fixes are
engine-level — every existing agent and every future Copilot agent inherits
them with zero YAML changes.

### Part H — hearing (pipeline / stream)

- **Covered-seconds plausibility** (H1): the words-per-duration hallucination
  guard now scores against `STTRouter.last_covered_seconds` (audio the
  streamed finalize actually covered), not the single VAD chunk. Live: real
  answers rejected as "14 words in 1440ms".
- **Barge-in keeps fed caller audio** (H2, `BARGE_KEEP_STREAM_AUDIO=true`):
  the queue flush no longer `reset_turn()`s the stream — the interrupting
  speech IS the next turn ("cleaning and helping…" and a spelled email
  fragment both vanished via the generation bump).
- **Late-segment stash** (H3): a non-empty stream segment arriving between
  turns is stashed 2.5s and served to the next finalize instantly (was:
  dropped as "stale inter-turn segment" while that same finalize sat in a
  2s timeout).
- **Consumed-episode empties** (H4): an empty finalize <3s after an
  over-covered transcript no longer counts as "unrecognized speech" (was:
  "आवाज़ स्पष्ट नहीं आई" to a caller who had been perfectly heard).
- **Continuation hold** (H5, `MERGE_HOLD_CONTINUATION=true`, 1200ms): a
  transcript ending in a dangling word (जो/कि/तो/and/because/અને…) never
  takes the idle-skip fast path — the resumed speech merges into one turn.
- **"हेलो" allowlisted** (H6): the spelling Sarvam actually emits joins the
  single-word greeting allowlist (the caller's are-you-alive probe was
  skipped; dead-air watchdog fired instead).
- **Condensed watchdog replay** (H7): dead-air reprompts >90 chars replay
  only the final sentence (the question), not the whole essay.

### Part C — capture ownership + state

- **Ask-time arming** (C1, `CAPTURE_ASK_ARM=true`): the engine detects an
  email-ask in the agent's own utterance (LLM or template) and arms capture
  immediately; rejection no longer releases it. `note_asked` was
  scheduler-only — LLM/stage-goal asks (the live norm) left capture dark.
- **Engine-owned readbacks** (C2): while armed, any email in LLM output is
  replaced with the spelled read-back of the engine's pending value (or a
  generic reference) — the LLM can never again "confirm" a value state does
  not hold. Progress queries ("नोट कर लिया?") answered from state. Runtime
  system-prompt doctrine injection covers EXISTING agents (never spell-slowly,
  never claim noted).
- **Read-back-context confirms** (C3): bare yes/no resolves a pending
  contact value only right after a read-back; "हाँ सही है सही है।"
  (all-affirmation-token turns) now confirms; a late "नहीं नहीं" answering
  "any more questions?" no longer discards the email. Applies to the generic
  confirm loop too. Confirmed values are immune to later negations.
- **Expectation-scoped affordances** (C4): the 2s capture merge window,
  en-IN STT forcing and raised barge threshold apply only while the engine's
  last utterance solicited spelling/confirmation — armed-but-digressing turns
  (Zoom, booking) pay normal latency (was: 2s tax on five closing turns).
- **Extraction hygiene** (C5): the LLM slot extractor is skipped during
  spelling turns (name='Panj Loa' from spelled "angelo"); the echo guard
  applies only to Latin-script caller turns (Devanagari "ज़ूम" answering
  "Zoom या Google Meet?" was dropped as an agent echo).
- **Answer-slot overwrite** (C6, `SLOT_ANSWER_OVERWRITE=true`): a caller
  directly answering the slot question the agent just asked may overwrite a
  filled non-contact slot via `source="answer"` (preferred_time='Now' was
  immortal; the booked "आज 4 बजे" never landed). Contact slots stay
  deterministic-only for "answer" too.
- **Normalizer** (C7): "एट द रेट"/"at the rate" variants → @; spelled
  material before an @-token joins the local part
  ("ए एन जी ई एल ओ। डॉट। पी ए। knaan@gmail.com" → angelo.pa.knaan@gmail.com).

### Knobs

`BARGE_KEEP_STREAM_AUDIO` (true), `MERGE_HOLD_CONTINUATION` (true),
`MERGE_HOLD_CONTINUATION_MS` (1200), `CAPTURE_ASK_ARM` (true),
`SLOT_ANSWER_OVERWRITE` (true). H1/H3/H4 ride `ENABLE_STREAMING_STT`;
C2–C5 ride `CAPTURE_MODE`. Compose env changes need `up -d`, not restart.

### Verification

Tests: `tests/test_hearing_capture_hardening.py` (43, seeded with this
call's exact ASR); full suite 800 green. On a live call grep for:
`[STT:Stream] late-segment stash`, `[ENGINE] Merge window held`,
`[CAPTURE] ask detected`, `[CAPTURE] LLM spoke an email`,
`[STATE] … source=answer`, absence of `Rejecting implausible` on
multi-chunk streamed turns. Note: `opening handshake failed` every 20s in
ws_audio_receiver.log is the docker healthcheck probing :9001 — cosmetic,
not the Sarvam stream.

## 22. Close-confirmation gate — platform-wide (2026-07-31)

**The call that caused it** (`a0f9a6ae`, 07:16 UTC, ws_audio_receiver.log).
Two independent defects, one after the other:

1. `07:16:14` STT merged segments #21 "Okay" + #22 "Bye" into `okay bye` —
   the caller was actually mid-sentence ("...don't mind giving it sir"
   arrived 1.3s later as #23). The deterministic farewell branch fired
   instantly: **"Thank you for your time."** + `hangup_pending`.
2. The caller kept talking, so the engine cancelled that hangup correctly at
   `07:16:18`. But the LLM's reply to the next turn — *"I understand… **Is
   there anything else I can help you with today?**"* — carried `[END_CALL]`.
   The engine trusted the marker on a turn that was an **open question**, and
   at `07:16:28` the bridge cut the line while the caller was answering it
   ("Can you take my email address?", segment #24 at `07:16:25`).

That is the "mysterious mid-conversation cut": nothing mysterious, the agent
was told to end and the caller's answer was still inside STT when it did.

**Fix — three layers, all default-on for every agent, no YAML edit needed:**

- **Confirm before ending** (`call_flow.termination.confirm_before_end`,
  default **true**). Every *speech-triggered* close now asks one question
  first — owner-specified copy since 2026-08-01: hi "ठीक है, तो फिर मैं कॉल
  कट कर दूँ?", en "Alright then, shall I end the call here?", gu equivalent
  (first-person subjunctive = gender-neutral; override per agent via
  `response_bank.localized_responses.close_confirm`) — and arms
  `hangup_pending` only on a clear yes. The confirmed close then speaks the
  short platform thank-you — hi "बात करने के लिए धन्यवाद, आपका दिन शुभ हो।"
  (`localized_responses.close_confirmed` overrides) — NOT the agent's
  `closing_responses`, because the goodbye may already have been spoken on
  the turn that triggered the confirmation; the closure prompt band also
  tells the LLM to keep its pre-`[END_CALL]` goodbye to ONE short sentence
  so farewells don't stack. `_is_close_confirm_yes` is biased
  toward keeping the call alive: a bare affirmation, a short affirmation-led
  turn (≤4 tokens), a farewell word or an explicit "end/cut/रख" wins; a
  negation, a question, or **anything with content in it** drops the gate and
  the turn is answered as normal conversation. Capped at
  `_CLOSE_CONFIRM_MAX = 2` asks per call so a caller who really is leaving is
  never trapped in a loop.
- **`[END_CALL]` on a question is ignored.** A turn whose text ends in `?` is
  by definition not a goodbye; the marker is dropped and logged
  (`[HANGUP] [END_CALL] ignored — turn ends in a question`). The prompt band
  gained a matching `✗ NEVER write [END_CALL] on a turn that asks the caller
  a question` rule.
- **The bridge waits for silence.** `_maybe_agent_hangup` now also requires
  `pipeline._pipeline_quiet(now)` — the engine only cancels a pending hangup
  when a *finalized* transcript reaches `process_stream`, so a caller who
  starts speaking during the drain window had not cancelled it yet. Bounded
  by `AGENT_HANGUP_MAX_WAIT_S` (20s, then `[HANGUP_FORCED]`) so a stuck
  VAD/STT flag cannot hold a dead call open. A **hard** close (max-duration
  cap) skips the check by design.

**Deliberately NOT gated** — these are not hearing judgements, and asking
again would be worse than the risk: DND / "stop calling me" and wrong-number
(compliance), silence hangup (nobody is there to answer), and
`max_call_minutes` (the platform is ending the call regardless).

**Copilot.** `confirm_before_end` documented in the schema block and the
ENDING CALLS doctrine; `_normalize_definition` coerces it to bool; a new
`_quality_warnings` lint fires when it is set false. New agents inherit the
gate with no configuration.

**Rollback.** Per-agent: `termination: {confirm_before_end: false}` restores
instant closes. Platform: `ENABLE_AGENT_HANGUP=false` still kills all
agent-initiated hangups; `AGENT_HANGUP_MAX_WAIT_S=0` disables the new quiet
wait alone.

### Verification

Tests: `test_call_termination.py` (+14, including a replay of both live
defects), `test_agent_hangup.py` + `test_continuation_merge.py` updated to
drive the two-turn exchange, `test_copilot_quality_lints.py` (+3), 90 golden
prompts regenerated for the two new closure-band lines; full suite **817
green**. On a live call grep ws_audio_receiver.log for
`[HANGUP] close-confirm asked`, `[HANGUP] close confirmed by caller`,
`[HANGUP] close-confirm answered … not a goodbye`, and
`[HANGUP] [END_CALL] ignored`.

## 23. FreeSWITCH event-socket credential (2026-07-31) — OUTAGE FIX

**The outage.** From 08:19 UTC (container recreate after the 08:03 reboot)
until 11:00 UTC, **every outbound call failed** — manual dial, campaigns,
follow-up automations, autodial, number test-call and the nightly loopback
probe. The dashboard returned `{"ok": true, "status": "dialing"}` and then
logged `DIAL <phone> from <did> (own-number) → failed`; FreeSWITCH's log
showed nothing at all, because no originate ever reached it.

Cause: the ESL hardening was applied to **code only**. `shared/fs_esl.py`
dropped the `ClueCon` default (`PASSWORD = os.environ.get("FS_ESL_PASSWORD",
"")`) and now rejects unset / vendor-default / <16-char values, but
`/etc/lescall/` had never been created on the host and
`/etc/freeswitch/autoload_configs/event_socket.conf.xml` still said
`password="ClueCon"`. `fs_api` raised before opening the socket,
`_fs_esl_exec` turned that into `-ERR esl transport failure: …`, and
`_parse_fs_result` read it as `failed`.

**The password lives in exactly two places and they must be rotated
together** — that is the whole point of the loud failure:

1. `/etc/lescall/telephony.env` → `FS_ESL_PASSWORD=` (root, 0600)
2. `/etc/freeswitch/autoload_configs/event_socket.conf.xml` → `password=`

```bash
PW=$(openssl rand -hex 32)
sudo install -d -m 0750 -o root -g root /etc/lescall
printf 'FS_ESL_PASSWORD=%s\n' "$PW" \
  | sudo install -m 0600 -o root -g root /dev/stdin /etc/lescall/telephony.env
sudo cp -a /etc/freeswitch/autoload_configs/event_socket.conf.xml{,.bak-$(date +%Y%m%d)}
sudo sed -i "s|name=\"password\" value=\"[^\"]*\"|name=\"password\" value=\"$PW\"|" \
  /etc/freeswitch/autoload_configs/event_socket.conf.xml
sudo systemctl restart freeswitch            # check `show channels count` first
sudo docker compose -f deploy/docker-compose.services.yml up -d
sudo systemctl restart lescall-sentinel
```

**`docker compose` must now be run with `sudo`.** The `env_file:` is read by
the **compose CLI as the invoking user**, not by the daemon as root — so a
plain `docker compose up -d` as `angelo` dies with
`open /etc/lescall/telephony.env: permission denied`. Keeping the file
root-0600 is deliberate (the repo is bind-mounted into every container as uid
1000, so anything under it is readable by the live-call runtime); running
compose as root is the price. Do **not** relax the file mode instead.

**Never pass `--remove-orphans`.** `lescall-pg` belongs to
`docker-compose.postgres.yml` and compose reports it as an orphan of the
`deploy` project. `--remove-orphans` deletes the database container.

**The host sentinel needs the secret too.** It runs as `User=angelo` under
systemd with only `.pg_env`, so `loopback_probe` (`shared/sentinel.py`)
could not originate. Fixed with a drop-in — systemd reads `EnvironmentFile`
as root *before* dropping privileges, so the secret stays root-only:
`/etc/systemd/system/lescall-sentinel.service.d/10-telephony-env.conf`.

**Consumers of 8021** (all host or `network_mode: host`): `dashboard/server.py`
(`_fs_esl_exec`), `shared/sentinel.py` (`loopback_probe`). `socket_server.py`
is **outbound** ESL — FreeSWITCH dials *out* to port 9000 from the dialplan,
so it never authenticates. That is why inbound calls kept working throughout
the outage while every outbound one failed.

### Verification

```bash
docker exec lescall-api python -c "import sys;sys.path.insert(0,'/home/angelo/lescall');\
from shared.fs_esl import fs_api;print(fs_api('status',10))"
calling-agent/venv/bin/python -m shared.sentinel --probe
```
The probe originates from each DID **to itself** (caller ID = the same DID,
`&park()`, legs killed afterwards) — it rings no human and is marked
`billable=0`. Expect `"ok"` per DID. Confirmed 2026-07-31 11:01 UTC:
`918065953160` ok, `912264231897` ok — answered + audio played.

**Still open**: `deploy/freeswitch-event_socket.conf.xml` (the hardened
template) also moves the bind from `::` to `127.0.0.1` and applies
`loopback.auto` ACL. Only the password was rotated in this fix; installing
the full template is a separate, FS-restarting change.

## 24. WebSocket streaming STT REMOVED — back to batch (2026-07-31)

Owner request. The saaras:v3 WebSocket provider that ran in front of the
batch STT since 2026-07-29 is **gone from the code**, not just switched off.
The live STT path is the batch multipart POST to
`api.sarvam.ai/speech-to-text` per finalized VAD segment — exactly what ran
before the streaming canary.

**Two steps, both done:**

1. `ENABLE_STREAMING_STT: "true"` removed from `deploy/docker-compose.services.yml`
   (runtime service) and `sudo docker compose … up -d runtime`.
2. The code deleted, so the flag no longer exists at all.

**Deleted**: `app/voice/stt/sarvam_stream.py` (476 lines, the live provider)
and `app/voice/stt/sarvam_ws.py` (216 lines, an earlier attempt that nothing
imported).

**Rewired**:
- `conversation_call_bridge.py` — the `if ENABLE_STREAMING_STT` branch is
  gone; the router's primary is always `TrackingSarvamSTT`.
- `pipeline.py` — `ENABLE_STREAMING_STT`, `BARGE_KEEP_STREAM_AUDIO` and the
  `_streaming_stt` handle removed, along with every feed/finalize site: the
  two `feed_nowait` calls in the VAD loop, the mid-turn `turn_text` feed to
  the TurnManager (proxy tokens again), the `reset_turn()` calls on
  too-short-discard and barge-in flush, and the `close()` in `stop()`.
- `stt/router.py` — `last_covered_seconds` and `set_stream_language`
  removed; `_derive_confidence` lost its `covered_seconds` parameter.

**Part H of §21 partly went with it.** H1 (covered-seconds plausibility), H2
(barge keep-audio), H3 (late-segment stash) and H4 (consumed-episode
empties) were mitigations for streaming-specific failure modes — a finalize
spanning several VAD chunks, segments draining after their finalize. On the
batch path a transcript covers exactly the segment it was POSTed for, so
those conditions cannot arise and the code was dead weight, not protection.
**H5 (continuation hold) and all of Part C (capture) are untouched.**

**Deliberately kept**: `LLM_TOOLS_MODE: "on"` (independent of STT),
`VAD_ENGINE: "fsmn"`, `ENABLE_CONTINUATION_MERGE`, the STT language pin
(`_update_stt_lang_pin` feeds `_stt_lang` on the batch request — it was
never streaming-only), and `endpointer.py` itself. The endpointer now always
receives `("", False)` and so decides on acoustics alone; its semantic
hold/fast-complete profiles need mid-turn text that only a streaming
provider could supply.

**Cost of the rollback**: the ~300-800ms overlap saving is gone. Every turn
now pays the full batch POST after the endpoint fires.

**To re-introduce streaming**, restore the provider from git history
(`git log -- calling-agent/backend/app/voice/stt/sarvam_stream.py`) — it is
no longer a flag flip.

### Verification

Full backend suite **812 green** (was 817: 5 streaming tests and 3
`covered_seconds` tests removed, 3 replacements added). In the live
container:

```bash
docker exec lescall-runtime python -c "import sys;\
sys.path.insert(0,'/home/angelo/lescall/calling-agent');\
import backend.app.voice.stt.sarvam_stream"   # → ModuleNotFoundError
```
On the next live call, `[STT:Stream]` and `[PIPELINE] Streaming STT ACTIVE`
must be absent from ws_audio_receiver.log; STT latency per turn returns to
the batch profile.

## 25. Billing, plans & subscriptions (2026-07-31) — SHIPPED, ENFORCEMENT OFF

Implements `deploy/PLAN-razorpay-subscriptions.md`. Everything below is in the
repo and inert: **no Razorpay account exists yet**, and the enforcement switch
is `false`, so the platform behaves exactly as it did before. Turning it on is
one DB flag (§25.6).

### 25.1 What shipped

| Piece | Where |
|---|---|
| Schema | `shared/db/migrations/0013_billing.sql` (+ `.down.sql`) — 10 tables |
| Boot ALTERs | `agents.archived_at`, `usage_ledger.entitlement_check` in `shared/db/database.py` |
| Entitlements | `shared/billing/entitlements.py` — the ONLY thing the hot path reads |
| Gateway | `shared/billing/razorpay_client.py` — inert and honest when unconfigured |
| Checkout/recovery | `shared/billing/service.py` |
| Webhooks | `shared/billing/webhooks.py` |
| Receipts (GST) | `shared/billing/receipts.py` |
| Sync + dunning | `shared/billing/sync.py`, own supervised process in `workers_entry.sh` |
| Tenant API | `dashboard/billing_api.py` → `/api/billing/*` |
| Admin plane | `dashboard/admin_app.py`, compose service `admin` |
| SPA | `frontend/src/pages/Billing.tsx`, `Receipt.tsx`, `api/billing.ts` |
| Tests | `tests/test_billing_core.py` (35, pure), `tests/test_billing_lifecycle.py` (29, PG) |

### 25.2 The plans (from the published pricing page, 2026-07-31)

Prices are **GST-EXCLUSIVE**; `plans.price_paise` stores the base and the
gateway is charged `base × 1.18`.

| | Free | Pilot | Basic | Growth | Enterprise |
|---|---|---|---|---|---|
| Price ex-GST | 0 | ₹2,999 once | ₹14,999/mo | ₹49,999/mo | custom |
| Charged | 0 | ₹3,538.82 | ₹17,698.82 | ₹58,998.82 | — |
| Minutes | 30/mo | 500 / 7 days | 3,000/mo | 8,000/mo | custom |
| Agents | 1 | 3 | 10 | 25 | unlimited |
| Recordings | ✗ | ✓ | ✓ | ✓ | ✓ |
| AI summaries | ✗ | ✓ | ✗ | ✓ | ✓ |
| Transcripts | ✗ | ✗ | ✗ | ✓ | ✓ |
| Conversation analytics | ✗ | ✗ | ✗ | ✓ | ✓ |
| Automated follow-ups | ✗ | ✗ | ✗ | ✓ | ✓ |
| Additional minutes | ✗ | ✗ | ✗ | ✗ | ✗ |

> ⚠ **Both monthly tiers are above the RBI AFA ceiling.** AFA-free e-mandate
> auto-debit stops at **₹15,000 per transaction, GST included**. Basic charges
> ₹17,699 and Growth ₹58,999, so a **card or UPI-Autopay mandate will demand a
> customer approval every single month** on both. The plan document assumed
> only Growth was affected — it is both.
>
> Mitigation, already wired: `/api/billing/plans` returns `afa_free:false` for
> these tiers, the plan card says so, and checkout offers **eMandate/eNACH**
> first (no per-debit AFA up to ₹99,999). Enable eMandate on the Razorpay
> account before selling either tier, or the dunning path
> (`pending → halted → grace → Free`) becomes the normal monthly experience.

**Additional minutes are NOT sold** (owner decision, 2026-08-01). Exhausting
the allowance offers exactly one route: upgrade. Every plan is seeded
`topup_allowed=0`, so `/api/billing/topup` refuses for everyone and the SPA
renders no purchase control. The Orders rail behind it is built and tested —
re-enabling is a data change (`UPDATE plans SET topup_allowed=1,
topup_pack_minutes=…, topup_rate_paise=…`), not a migration.

> The published pricing page still advertises "Additional Usage ₹8/minute"
> (Basic) and "₹6/minute" (Growth). Those two lines should come off the page,
> or it promises something the product deliberately does not do.

Agent caps are the plan document's D2 defaults — the pricing page states no
numbers. Change them in `plans` and re-run `resolve_entitlements`; no code
change is needed.

### 25.3 Setting up Razorpay (when the account exists)

1. KYC-verified account → enable **Subscriptions** → Account Settings → GST
   details → enable cards, **UPI Autopay** and **eMandate/eNACH**.
2. Test keys into `/etc/lescall/billing.env` (see
   `deploy/etc-lescall-env.template`), then
   `sudo docker compose -f deploy/docker-compose.services.yml up -d`
   (**`up -d`, not `restart`** — env_file is only re-read on recreate).
3. Create the two recurring gateway Plans:
   ```bash
   docker exec -w /home/angelo/lescall lescall-api \
     python -m shared.billing.plans_bootstrap          # dry run first
   docker exec -w /home/angelo/lescall lescall-api \
     python -m shared.billing.plans_bootstrap --apply
   ```
   **Razorpay Plans are immutable forever.** A price change means a new plan id
   and re-authorising every subscriber's mandate. Pilot and top-ups need no
   Plan — they are Orders created per purchase, so their price can change freely.
4. Webhook → `https://agent.lescall.com/api/billing/webhook/razorpay`, with its
   **own secret** (≠ API secret, and different again in live). Subscribe to:
   all `subscription.*`, `order.paid`, `payment.captured`, `payment.failed`,
   `refund.processed`, `invoice.paid`.
5. Install the nginx rate limit + 256k body cap for that location
   (`deploy/nginx-agent.lescall.conf`), then `sudo nginx -t && sudo systemctl reload nginx`.

### 25.4 GST invoicing

Ours is the **tax invoice**; Razorpay's auto-invoice is only a payment receipt
(no customer GSTIN, no SAC, no CGST/SGST split). Set `PLATFORM_GSTIN`,
`PLATFORM_LEGAL_NAME`, `PLATFORM_ADDRESS`, `PLATFORM_STATE_CODE` in
`/etc/lescall/billing.env` — `/api/billing/health` reports `invoicing_ready`
and names the missing field.

- Numbering is a consecutive per-FY series (`LC-26-27-00001`), allocated with
  `SELECT … FOR UPDATE` **inside the payment transaction**, and only when the
  payments INSERT actually inserted — so a webhook retry cannot burn a number.
- Credit notes (`LCCN-…`) are issued automatically on a full refund. An issued
  invoice is never deleted.
- The customer's details are **snapshotted onto the payment row at issue**, so
  editing a billing profile later cannot rewrite an invoice already issued.
- GSTIN is validated server-side including the mod-36 check digit, and the
  place of supply is **derived from it** — never accepted as a separate field.

### 25.5 When a payment goes wrong

Every case below is handled; this is where to look when a customer asks.

| Symptom | What happens | Where |
|---|---|---|
| "I paid, nothing happened" | tenant presses **I paid but nothing happened** on `/billing` → polls the gateway → recovers | `POST /api/billing/reconcile` |
| Customer closed the tab after paying | webhook opens the window anyway | `webhooks._settle_order` |
| Webhook lost / not configured | sync recovery pass every 15 min | `sync.pass_recover_payments` |
| Card declined | attempt `failed` + notification; plan untouched | `webhooks._on_payment_failed` |
| Renewal fails | `pending → halted → 72h grace → Free`, and the gateway sub is **cancelled** so they can subscribe again | `sync.pass_grace_expiry` |
| Paid twice | idempotent on `razorpay_payment_id`; one receipt only | `service.record_payment` |
| Amount ≠ plan price | quarantined, nothing granted, alarm | `webhooks._record_subscription_charge` |
| Webhook replayed | deduped on `event_id`, one effect | `webhooks.ingest` |
| Webhook for an unknown id | recorded + alarmed, never granted | `webhooks._unknown` |
| Refund | payment flipped, credit note, minutes recomputed | `webhooks._on_refund` |
| Handler crashed | event left unprocessed, replayed from the stored raw payload | `sync.reprocess_stuck_events` |

Operator views: `GET /admin/api/billing-events?unprocessed_only=1`,
`GET /admin/api/tenants/{owner}` (subscriptions + payments + **attempts**), and
`POST /admin/api/tenants/{owner}/reconcile`.

Every checkout attempt — including the ones that never become money — is a row
in `payment_attempts` with `failure_step` / `failure_code` / `failure_reason`.
That table, not `payments`, is what answers "why did their card fail".

### 25.5b Rollout record — what actually happened (2026-08-01)

Deployed P0–P5 live. **Enforcement is OFF**; flipping it is §25.6.

Three things bit during the rollout; all are fixed, and all are worth knowing:

1. **The 0013 staff seed did not survive.** It wrote
   `entitlements.plan_code='enterprise'` with no `overrides`, and
   `resolve_entitlements()` rebuilds the row wholesale from the
   `subscriptions` history — these accounts have no subscription, so the first
   resolve correctly reset all three staff tenants to Free. Fixed by
   **migration 0014**, which writes the grant as an `overrides` JSON (the
   supported mechanism, and what the admin plane uses) and additionally gives
   every other pre-existing account an explicit Free row. Verified stable
   across repeated re-resolution.
   *Rule: to grant a plan without a gateway subscription, write `overrides`.
   A bare `plan_code` is derived state and will be overwritten.*

2. **`/run/lescall-admin` is tmpfs** — it vanishes on reboot and the admin
   container then crash-loops on `PermissionError`. Made persistent with
   `/etc/tmpfiles.d/lescall-admin.conf`.

3. **A test fixture had written `billing_enforce` into the PRODUCTION settings
   table** (found stamped `by: "pytest restore"`). Cause: `enforcement_on` and
   `_use_test_db` were both autouse and function-scoped, so pytest was free to
   run the former first, while `LESCALL_DB_URL` still pointed at production.
   Fixed by making `enforcement_on` take `_use_test_db` as an explicit
   dependency. Any future billing fixture that writes MUST do the same.

Verified live after deploy: all six containers healthy · billing endpoints 401
without a session · `…/webhook/razorpay` 503 (no secret) while
`…/webhook/razorpay-test` is 401, proving `_OPEN_EXACT` · checkout answers a
clean 503 rather than crashing · every pre-existing surface still 200 ·
9000/9001 bound to 127.0.0.1 · admin reachable only over the UNIX socket
(TCP refused) · **loopback call probe OK on both DIDs, answered + audio**, and
those calls metered with `entitlement_check='ok'` and released their
reservations.

### 25.5c Open signups + the telephony gate (2026-08-01)

`LESCALL_ALLOWED_EMAILS=*` — any verified Google account may register again.

What a brand-new account gets: **Free** (30 min/month, 1 agent) and **no
telephony**. They can build and test an agent in the dashboard; they cannot
register a SIP trunk or attach a DID. Connecting a real number starts at Pilot.

Migration **0015** adds `plans.telephony_enabled` / `entitlements.telephony`
(Free 0, Pilot/Basic/Growth/Enterprise 1) and grandfathers anyone who already
owned a trunk or number — via `overrides`, so it survives re-resolution.

Enforced by `telephony_api.paid_owner`, applied to the six MUTATING endpoints:

| Gated (402) | Open (200) |
|---|---|
| `POST /api/trunks` · `DELETE /api/trunks/{id}` · `POST /api/trunks/{id}/apply` | `GET /api/trunks` |
| `POST /api/numbers` · `PATCH /api/numbers/{id}` · `DELETE /api/numbers/{id}` | `GET /api/numbers` · `GET /api/trunks/{id}/instructions` · `GET /api/telephony/server-info` |

Reads stay open so the SPA can render a locked panel that shows what the plan
unlocks (`TelephonyLocked` in `frontend/src/pages/Telephony.tsx`); a 402 on the
list call would leave it nothing to draw but an error.

> **This gate deliberately ignores `enforcement_enabled()`.** That switch stages
> quota rollout for tenants who already have plans. This one keeps strangers off
> the carrier edge and has to bite the moment signups reopen. Everything else in
> §25.9 still no-ops while enforcement is off.

Every account now also gets an **explicit** entitlements row at first login
(`auth._upsert_user` → `resolve_entitlements`), so the P6 discovery query keeps
returning zero.

⚠ **With enforcement still OFF, a new signup gets unlimited agents and
unlimited Copilot turns.** The Copilot's model quota is shared with live calls,
so that is the remaining exposure from open signups — see §25.6.

### 25.5d BYON in containers — three gaps found live (2026-08-01)

A tenant's trunk "Apply" 500'd. The trunk and number saved fine; only the final
step failed. Three independent containerization gaps, all now fixed:

1. **`/etc/freeswitch` was not mounted into `lescall-api`.** Apply writes the
   generated gateway/dialplan XML there. Fixed: bind mount + `group_add: ["994"]`
   (the dirs are `freeswitch:freeswitch drwxrwxr-x`).
2. **`_fs_reload()` shelled out to `fs_cli`**, which is not installed in the app
   containers — the same gap `shared/fs_esl.py` exists for. Every reload raised
   FileNotFoundError, so `reloaded` was False and apply reported
   `status: "error"` even with the XML written. Now uses the event socket,
   falling back to `fs_cli` for host-side callers.
3. **Generated files were written 0644**, so only the tenant who first claimed a
   DID could ever re-apply it. When a DID moves between tenants the old
   `ai_inbound_<did>.xml` is still owned by `freeswitch` and cannot be
   overwritten. Now written 0664 via `freeswitch_config._write_shared()`.

**Then the call itself failed with SIP 403** — carrier-side, not ours. The
diagnostic that pins it down: a 403 with **no preceding 407/401** means the
carrier rejected the SOURCE, not the credentials. On Plivo Zentrunk that is the
**termination** side: the trunk needs an IP ACL containing this server's egress
IP (`35.207.225.237/32`). Origination (`<ip>:5080`) is a separate setting and
configuring only that gives working inbound and 403 on every outbound.

Also added: **`PATCH /api/trunks/{id}`**. Previously a trunk could only be
created, applied or deleted, so fixing a wrong SIP domain or adding credentials
meant delete-and-recreate — which drops the DIDs and their agent bindings.
`name` stays immutable (it is the FreeSWITCH gateway name and filename), and a
masked `***` password is ignored rather than wiping the stored credential.

### 25.5e DID routing isolation — cross-tenant leak (2026-08-01)

`agents.is_active` is a SINGLE GLOBAL FLAG that every tenant's Activate button
writes. Four tenants had activated agents. Two paths could therefore hand a
call to the wrong tenant:

* a **registered DID with no agent bound** fell through to the global "active
  agent" lookup — so a customer calling tenant A's number would have been
  answered by tenant B's agent, speaking B's company name, billed to B;
* the last-resort lookup itself ordered by `updated_at DESC` across all owners.

Fixed in `conversation_call_bridge.py`:

* a registered-but-unbound DID now returns the `UNROUTED_AGENT` sentinel, which
  `_build_agent_stack` maps to the neutral fallback persona
  (`agent_health='unrouted'`), and `_did_owner()` still attributes the call to
  the number's tenant so quota and ledger land correctly;
* `_resolve_active_agent_slug()` is scoped to `LESCALL_DEFAULT_OWNER` — it
  exists for the founding tenant's pre-tenancy numbers and must never select
  another tenant's agent.

Pinned by `tests/test_did_routing_isolation.py`. That file imports the bridge
**inside a fixture**: a module-scope import pulls in the audio stack, whose
threads segfault the interpreter at shutdown (tests pass, then exit 139).

### 25.5f Manual invoices — money collected outside the gateway (2026-08-01)

Cash, NEFT/RTGS, cheque, or an account put on credit. `shared/billing/invoices.py`
+ the admin console at `/admin/tenant/<email>` → **Issue an invoice**.

Owner-approved design decisions:

| Decision | Choice |
|---|---|
| Unpaid ("yet to be paid") | **Activates the plan immediately**, balance shows as outstanding |
| GST numbering | Unpaid invoices take a **real number** in the `LC-<fy>-NNNNN` series on issue; cancelled by credit note, never deleted |
| Amount | Pre-filled from the plan, **editable** (negotiated + Enterprise deals) |
| Partial payments | Not in v1 — issue two invoices for a 50/50 split |

Rules the module enforces, each for a reason:

* The invoice number comes from the **issue date's** financial year, not
  today's. Back-dating across a FY boundary is refused outright — it would
  insert a number into a series that has already been filed.
* Future-dated invoices are refused.
* The plan grant is written into `overrides`, never as a bare `plan_code` —
  entitlements are derived state and a bare plan_code does not survive the next
  `resolve_entitlements()` (see 0014).
* `payments.razorpay_payment_id` is UNIQUE and is the gateway idempotency key,
  so a manual invoice gets a synthetic `manual-<hex>` value rather than NULL
  (a nullable UNIQUE column admits unlimited NULLs).
* The invoice line names the **plan actually sold**, captured in the snapshot
  at issue — it previously said "Enterprise" for every manual invoice.

Operator flow: **Issue** → (if on credit) **Mark paid** with method + UTR/cheque
ref → or **Void**, which allocates an `LCCN-<fy>-NNNNN` credit note and requires
a reason. `/admin/invoices` is the collections view (unpaid, oldest first,
overdue flagged).

Tenant side: the same invoice appears in their **Billing → Payment history**
with `paid` / `payment due` / `cancelled`, an outstanding-balance banner, and
the printable GST invoice at `/billing/receipt/<id>`.

Migration **0016** adds `issued_at`, `due_date`, `paid_at`, `payment_reference`,
`created_by`, `voided_at`, `void_reason`, and extends `payments.status` with
`due` and `void`.

> ⚠ **Invoicing is BLOCKED until the supplier identity is set.** `issue()`
> refuses while `PLATFORM_GSTIN` / `PLATFORM_LEGAL_NAME` / `PLATFORM_ADDRESS` /
> `PLATFORM_STATE_CODE` are blank in `/etc/lescall/billing.env`, naming the
> missing field. A document with no GSTIN or place of supply is not a tax
> invoice — it burns a number out of the GST series for something the customer
> cannot claim credit against and we cannot file.
>
> That guard was added after the fact: three test invoices (₹58,998 · ₹14,160 ·
> ₹58,998) reached a live tenant's Billing page during this feature's own
> testing. They were hard-deleted and `receipt_counters` reset to 0 for FY
> 26-27, so the first REAL invoice is `LC-26-27-00001`. Hard delete rather than
> the usual credit-note reversal was correct **only** because the platform
> GSTIN had never been configured: nothing had been filed, so no series
> existed to protect, and voiding would have left the customer looking at three
> invoices plus three credit notes for money they never paid. Once a real
> invoice exists, the rule is absolute — cancel with a credit note, never
> delete.

### 25.5g Admin console — operations surfaces (2026-08-01)

`dashboard/admin_ops.py`. Everything below previously needed SSH, `psql` or
`docker ps`. Ten pages total; nav across the top.

| Page | Answers |
|---|---|
| `/admin` | tenants — search, plan, status, minutes, agents, calls, telephony |
| `/admin/tenant/<email>` | everything about one account + plan/lock/invoice controls |
| `/admin/health` | is the platform up — listeners, live calls, freshness, disk, billing integrity |
| `/admin/keys` | **Sarvam key pool** — the highest-impact number on the platform |
| `/admin/calls` | cross-tenant call browser with billing attribution |
| `/admin/agents` | every agent, owner, orphans, legacy active flag |
| `/admin/audit` | who did what, including admin reads |
| `/admin/invoices` | outstanding balances, oldest first |
| `/admin/payments` | all money in |
| `/admin/events` | gateway webhook events, stuck ones flagged |

**Why the key pool page matters most.** Sarvam supplies STT and TTS for every
call. When the pool is exhausted, *every tenant's calls go silent* — and since
roles were flattened, `/api/sarvam/*` is 403 for every session, so there was no
UI for it anywhere. Current state: 13 usable, 11 already exhausted. Rotation is
automatic on HTTP 429; the page exists to top the pool up BEFORE that runs dry.

Two traps that bit while building it, both worth remembering:

* `api_key_pool.json` is `{"pool": [...]}`, not a bare list, and has THREE
  other consumers — `sarvam_key_watchdog.py`, `key_rotator.py` (in the live
  call path) and `server.py`. The admin page therefore calls
  `server._load_api_key_pool` / `_save_api_key_pool` rather than writing the
  file itself; a bare-list write would have silently broken rotation.
* `sarvam_dead_keys.json` wraps its list under `"dead"`. Reading the wrapper as
  the list reported **1** exhausted key when there were **11** — a number an
  operator would have acted on.

**The admin plane deliberately gets no new credentials.** It reads ports by
connecting to them, live state from the repo mount, and everything else from
the database. It does NOT take `FS_ESL_PASSWORD`: it can already change plans
and issue invoices, and adding "can originate calls" to a leaked `ADMIN_TOKEN`
buys only a channel count that port 8021 plus recent call activity already
imply.

### 25.6 Turning enforcement on (P6)

**Run the discovery query first.** Every pre-existing tenant must already have
an entitlements row, or they drop to Free (30 min, 1 agent) and block instantly:

```sql
SELECT DISTINCT owner FROM (
  SELECT email AS owner FROM users UNION SELECT owner FROM agents
  UNION SELECT owner FROM usage_ledger UNION SELECT owner FROM calls) t
WHERE owner <> '' AND owner NOT IN (SELECT owner FROM entitlements);
-- MUST return zero rows.
```

Migration 0013 seeds `tech@`, `angelo@` and `alwynantonyben@dhronai.com` as
Enterprise/unlimited. Anyone else must be granted a plan from the admin plane
first.

```bash
# apply the schema (additive; enforcement still off)
docker exec -w /home/angelo/lescall lescall-workers python -m shared.db.migrate --status
docker exec -w /home/angelo/lescall lescall-workers python -m shared.db.migrate

# flip enforcement (admin plane, audited, reason mandatory)
curl --unix-socket /run/lescall-admin/lescall-admin.sock \
  -X POST http://local/admin/api/enforcement \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"enabled": true, "reason": "P6 enforcement go-live, owner approved"}'
```

Verify it reached **all four** services — the runtime container reads the same
DB setting but caches it for 30s, and inbound enforcement lives there:

```bash
curl -s localhost:8080/api/health | jq .billing
```

`billing.fail_open` must stay empty. A non-empty value means calls are being
admitted without a verifiable entitlement because the billing DB is erroring.

**Rollback** is the same call with `"enabled": false`.

### 25.7 Sync worker

Own supervised process inside `lescall-workers` (never the 5-minute tick — N
sequential gateway calls would make tick duration a function of Razorpay's
latency, and the sentinel restarts the container when `reconcile:` vanishes for
20 minutes).

```bash
docker logs lescall-workers 2>&1 | grep 'billing sync'
docker exec -w /home/angelo/lescall lescall-workers python -m shared.billing.sync --once
```

Alarms are logged as `[BILLING-ALARM]` — the sentinel and GCS log shipping
already watch that stream. They are deliberately not bell-only.

### 25.8 Admin plane

See `deploy/etc-lescall-env.template`. Refuses to start without a ≥32-char
`ADMIN_TOKEN` and refuses any bind that is not the UNIX socket or loopback.
No nginx vhost, no DNS, no firewall opening.

It binds BOTH interfaces: the UNIX socket always, plus loopback TCP when
`ADMIN_TCP_ENABLED=true`. The socket is the security-relevant one (an HTTP-URL
SSRF cannot address a UNIX socket at all); the TCP port exists only so a
browser at the far end of an SSH tunnel can render the page.

```bash
sudo install -d -m 0770 -o angelo -g angelo /run/lescall-admin   # + tmpfiles.d
sudo docker compose -f deploy/docker-compose.services.yml up -d admin
```

**From the VM (scripts, no browser) — the socket:**

```bash
TOK=$(sudo grep -oP '(?<=^ADMIN_TOKEN=).*' /etc/lescall/admin.env)
curl --unix-socket /run/lescall-admin/lescall-admin.sock \
     http://local/admin/api/health -H "Authorization: Bearer $TOK"
```

**From your laptop (browser) — SSH tunnel:**

```bash
ssh -L 8090:127.0.0.1:8090 angelo@35.207.225.237      # keep this open
```
then open <http://127.0.0.1:8090/admin>, paste the token into the sign-in form.

The form POSTs the token in the request BODY (never a query string, so it stays
out of access logs, shell history and Referer) and sets an 8-hour HttpOnly,
SameSite=strict cookie containing a **hash** of the token — so a stolen cookie
cannot be replayed as an API bearer token, and rotating `ADMIN_TOKEN`
invalidates every session for free.

**The cookie authorizes GET only.** Every mutation still requires the
`Authorization: Bearer` header, which is what keeps CSRF structurally
impossible: while the tunnel is open, any page the operator visits could POST
to 127.0.0.1:8090, but it cannot set that header. The HTML UI is read-only by
design, so it loses nothing; changes go through `curl` against `/admin/api/*`.

Turn the TCP bind off again with `ADMIN_TCP_ENABLED=false` + `up -d admin`;
the socket keeps working.

Reads are audited as well as writes: if the token leaks, reads are what an
attacker uses first. Every mutation needs a `reason` of ≥10 characters, and
`pilot-reset` additionally needs `confirm_owner` equal to the path owner so a
body-less SSRF POST cannot trigger it.

### 25.9 Enforcement points

`E1` manual dial · `E2` campaign (start **and** per contact) · `E3` autodialer ·
`E4` automation callback · `E5` number test call · `E6` inbound bridge ·
`E7` agent creation · `E8` autocall staging · `E9` copilot chat ·
`E10` copilot try-agent · `E11` document extraction · `E12` per-tier feature
flags at the API.

The grep gate — every `_fs_esl_exec(_fs_originate_cmd(...))` site must be
preceded by a check:

```bash
python3 - <<'PY'
src = open('dashboard/server.py').read().split('\n')
for i, l in enumerate(src):
    if '_fs_originate_cmd(' in l and 'fs_esl_exec' in l and not l.strip().startswith('#'):
        assert '_billing_check_dial' in '\n'.join(src[max(0,i-60):i]), f'UNGATED line {i+1}'
print('grep gate OK')
PY
```

`shared/sentinel.py`'s loopback probe is exempt by design: it is a synthetic
self-test marked `billable=0` that rings no human.

---

## 26. Share an agent by link — IMPORT makes an independent copy (2026-08-01)

A tenant hands someone a URL; the recipient signs in and imports a **copy** of
that agent into their own account. The two records are unrelated from the
moment of import: separate slug, separate `agent_id`, separate revision
history, separate owner. Editing one never changes the other, and archiving
one never touches the other.

### 26.1 Surfaces

| Where | What |
|---|---|
| Build → Agents card | Share icon (top-right of the card) → link dialog |
| `https://agent.lescall.com/s/<token>` | the link that gets sent |
| `/build/agents?import=<token>` | where the link lands; opens the import popup |
| `dashboard/agent_share_api.py` | `/api/share` (create/read) · `/api/share/{token}` (preview, revoke) · `/api/share/{token}/import` |
| `frontend/src/components/ShareAgent.tsx` | both dialogs |
| migration `0017_agent_share` | `agent_shares`, `agent_share_imports` |

### 26.2 The unit is the FAMILY

One link carries every direction variant the agent has (`x-inbound` +
`x-outbound`), because the plan cap counts families and the card in the UI is
a family. One import therefore consumes exactly one agent slot.

### 26.3 "The same agent cannot be shared by two users"

Three facts, no extra column:

1. `agents.slug` is globally UNIQUE and each row carries exactly one `owner`.
2. `POST /api/share` verifies every slug in the family is owned by the caller.
3. `idx_agent_shares_live` — `UNIQUE (owner, family) WHERE revoked_at IS NULL`
   — allows one live link per agent. Pressing Share twice returns the SAME
   token rather than minting a second, separately-revocable link.

An import mints a brand-new family, so two accounts never point at one row.

### 26.4 What travels, and what deliberately does not

Travels: `agent.yaml` (behaviour, personality, stages, knowledge base) and
`documents/` including the `.npy` embeddings — otherwise the copy references
knowledge it does not have and reads as broken.

Does **not** travel:
* `routing.yaml` — the sharer's DID binding. Copying it would point the
  importer's new agent at someone else's phone number.
* `bookings.json` — the sharer's live appointments, i.e. their customers.
* `revision` / `updated_at` / `updated_by` — a copy claiming "revision 47,
  updated_by someone@else.com" makes the importer's first edit look like a
  rollback.
* `is_active` — imported rows are forced to 0. `agents.is_active` DEFAULTS to
  1 and `conversation_call_bridge` picks the most recently updated `is_active`
  row per owner for calls that match no DID binding, so without this, importing
  an agent to look at it would silently take over answering that account's
  unbound calls.

`agent_id` is **reminted** (`<new-slug>-v1`, uniqueness-checked against every
`agent.yaml` on disk). `documents/store.get_folder_by_agent_id()` resolves a
document folder by scanning for the FIRST matching `agent_id`, so two agents
sharing an id means one tenant's RAG lookups can land in another tenant's
documents.

### 26.5 Refusals the recipient sees

| Situation | Result |
|---|---|
| At the plan agent cap | popup still renders the agent, Import replaced by **See plans** (402 `cap_agents` on the API) |
| Own link | 409 "this is your own agent" |
| Already imported (copy still live) | ~~409 + a link to their copy~~ → a **choice**: replace that copy, or import as new (§52). A bare POST still 409s. |
| Revoked / expired | 410, with which one it was |
| Source archived or deleted | 410 |
| >20 imports in 24h | 429 (abuse ceiling, `LESCALL_MAX_IMPORTS_PER_DAY`) |

Cap and already-imported are reported in the preview BODY (`can_import:false`
+ `reason`) rather than as HTTP errors, because the popup has to render the
agent's details alongside the explanation.

### 26.6 Atomicity

`agent_share_imports` has `UNIQUE (share_id, importer)` and the row is
**reserved before any folder is written**. That is what makes a double-clicked
Import button, a reloaded tab and two browser windows converge on one copy
instead of three. Any failure mid-import rolls back every folder, every
ownership row and the reservation together — half an imported twin pair is
worse than none, because a number could bind to the survivor and run the wrong
direction's prompts. Re-importing is allowed once the earlier copy has been
archived or deleted (the stale row is dropped inside the same transaction).

> **Superseded by §52 (0029):** the unique key is now `(share_id, importer,
> copy_no)`, stale rows are KEPT, and the guard this paragraph describes only
> ever covered two requests reaching the INSERT together — an ordinary
> double-click got through. See §52.4.

### 26.7 Deep links now survive login — platform-wide

A share link's entire payload is the token in its URL, and the nginx
`auth_request` wall used to throw the path away:
`error_page 401 → @lescall_login → return 302 /login`. Everyone landed on the
homepage after signing in and the invitation silently evaporated.

Three coordinated changes:

* `deploy/nginx-agent.lescall.conf` → `return 302 /login?next=$request_uri;`
* `auth.safe_return_path()` — accepts same-origin ABSOLUTE PATHS only. `//host`,
  `https://host`, backslashes, `/login*` and `/auth/*` are rejected, so `next`
  cannot be turned into an open redirect.
* `/login` stashes it in the short-lived httponly `lescall_return_to` cookie;
  `/auth/callback` redirects there instead of `/`.

Share links are `/s/<token>` — one path segment, no query string — precisely
because stock nginx has no URL-encoder, so a destination carrying its own `&`
would be truncated in `$request_uri`.

**Installing the nginx change** (root):

```bash
sudo cp deploy/nginx-agent.lescall.conf /etc/nginx/sites-enabled/agent.lescall
sudo nginx -t && sudo systemctl reload nginx
```

Without it, share links still work for anyone already signed in; a logged-out
recipient lands on the dashboard homepage and has to re-open the link.

### 26.8 Deploy

```bash
set -a; . deploy/.pg_env; set +a
./calling-agent/venv/bin/python -m shared.db.migrate --status   # 0017 should show pending
./calling-agent/venv/bin/python -m shared.db.migrate            # apply it
sudo docker restart lescall-api               # picks up the new router
docker compose -f deploy/docker-compose.services.yml up -d --build spa
```

Deployed 2026-08-01. Two traps hit on the way, recorded so they are not hit twice:

* The backend is the **`lescall-api` container** (bind-mounted, host network,
  port 8080). The old `lescall-dashboard` **systemd unit is disabled and must
  stay stopped** — starting it spawns a second `server.py` that loses the race
  for port 8080 and crash-loops under systemd's restart policy until stopped.
* `deploy/nginx-agent.lescall.conf` ends with a `default_server` 444 catch-all.
  `/etc/nginx/sites-enabled/lescall` (the legacy proxy-everything vhost)
  **claimed `default_server`**, so installing the file verbatim fails `nginx -t`
  with a duplicate default server. Resolution (2026-08-01, owner-approved): the
  vhost was installed with the catch-all block stripped, then the catch-all was
  installed separately as `/etc/nginx/sites-enabled/000-lescall-default` and the
  legacy vhost removed (backup: `/etc/nginx/lescall.legacy-vhost.bak-20260801`).
  Bare-IP / stale-DNS requests on :80 now get the connection closed with no
  response; only `Host: agent.lescall.com` is served. Note the 444 block covers
  **port 80 only** — on 443 the agent.lescall server is the sole listener and
  therefore still answers any SNI/Host with its own cert (pre-existing).

Until 0017 is applied every `/api/share` route answers **503 "Agent sharing is
not enabled on this server yet"** rather than a 500 about a missing relation.

### 26.9 Tests

`calling-agent/backend/tests/test_agent_share.py` (34). Pure tier covers the
slug/agent_id/spec algebra and the open-redirect guard; the DB tier runs the
full create → preview → import path against `lescall_test` with `agents/`
redirected at a tmp tree. The load-bearing ones: independence after import,
`is_active` stays 0, routing/bookings excluded, double-import, re-import after
archive, revoked/expired/deleted-source, name-collision walk, cap refusal, and
"a failed import leaves nothing behind".

---

## 27. LLM cost telemetry + prompt-cache readiness (2026-08-11) — AUDIT + SHIPPED

Before this, the platform measured **half** its AI bill. `credit_tracker.py`
knew every call's Sarvam credits to six decimals and recorded **zero LLM
tokens**, so "what does a minute cost?" was unanswerable for the model that
does the talking. This section is the audit that closed that gap and what it
found.

### 27.1 The measured cost of a call-minute

All figures measured, not modelled. Sarvam from 46 days of `credit_log/*.jsonl`
(3,375 real calls), turn rate from `call_turns` (106 calls / 196 min), token
counts from the live endpoint.

| Component | Rate | Measured usage | ₹/min |
|---|---|---|---|
| Sarvam STT `saarika:v2.5` | ₹30/hr, per-request ceil | 15–20 billed-sec/min | **0.14** |
| Sarvam TTS `bulbul:v3` | ₹3/1K chars | ~650 chars/min | **1.95** |
| Whisper fallback | ₹0.53/min | ~0 (never fires) | 0.00 |
| LLM `gemini-3.5-flash` | $1.50/$9.00 per 1M | 3,311 in / 25 out per turn × 4.8 turns/min | **2.20**¹ |

¹ **at list price. Actual LLM spend today is ₹0 — see 27.3.**

Per-day Sarvam is stable at ₹1.93–2.19/min across 29 Jul – 8 Aug. **TTS is 93%
of the Sarvam bill**; STT is noise.

### 27.2 Where the LLM money is: the prompt, re-sent every turn

Input is ~98% of the LLM bill. One turn = 3,311 input tokens vs 25 output. The
system prompt is 12,724 characters ≈ 3,236 tokens and it is re-sent on all 4.8
turns/minute.

`_build_system_prompt` therefore emits its blocks in three bands — STATIC,
PER-CALL, PER-TURN — so a provider prefix cache can serve the stable part.
Measured across the golden fixtures (6 stages × 3 languages × 2 directions):

    86% of the prompt is byte-identical across every stage
    = 2,708–2,946 tokens, diverging exactly at "━━ CALL STATE ━━"

That property is the entire precondition for prompt caching and nothing else in
the repo checked it — a timestamp in the identity block would silently take it
to zero. `tests/test_prompt_cache.py` now pins it, and `prompt_cache.
prefix_fingerprint()` logs a hash of the static band on every turn so a break
is visible in production the turn it happens.

### 27.3 The blocker: the Gemini key is on the FREE TIER

Probed live 2026-08-11 against `LLM_API_KEY`:

| Probe | Result |
|---|---|
| 6 consecutive byte-identical 3,226-token prefixes | `cachedContentTokenCount = 0` every time |
| `cachedContents.create`, all 4 fleet models | `429 TotalCachedContentStorageTokensPerModelFreeTier limit exceeded: limit=0` |
| `generateContent` burst | `429 … quotaValue: '5'` — **5 requests/minute** |

Three consequences, in order of severity:

1. **5 RPM is a hard scaling ceiling.** One call consumes 4.8 LLM turns/minute.
   The platform can serve roughly **one concurrent call** before turns start
   429ing. Today that is invisible because volume is low
   (`llm_fallback_turns > 0` on 3 of 419 calls in 30 days).
2. **Prompt caching is unavailable by policy, not by misconfiguration.**
   Implicit and explicit both. No application code can change `limit=0`;
   it needs billing enabled on the Google Cloud project.
3. **LLM spend is currently ₹0.** The ₹2.20/min above is what the *same
   traffic* costs the day billing is switched on — which it must be, per (1).

### 27.4 What enabling billing is worth

Measured token counts through the rate card in `shared/ai_pricing.py`:

| | per turn | per call-minute | platform total |
|---|---|---|---|
| no prefix cache | ₹0.4586 | ₹2.20 | ₹4.29/min |
| 2,800-token prefix cached | ₹0.1247 | ₹0.60 | **₹2.69/min** |

**−73% of the LLM bill, −37% of the platform bill**, ₹1,603 saved per 1,000
call-minutes. Sarvam's ₹2.09 is unaffected.

Order of operations matters: enable billing → implicit caching begins serving
the already-ordered prefix with no code change. Do not reorder the prompt to
chase this; it is already ordered.

### 27.5 What was deliberately NOT built

Measured first, then declined — each of these is a standard optimisation that
this codebase's data does not support:

| Not built | Why — measured |
|---|---|
| Response/semantic cache | 2,621 agent utterances over 45 days: **85.5% are unique**. Nothing to hit, and replaying a stale reply into a live call is a correctness hazard for a ~0% gain. |
| Wider TTS cache | Only **7.5%** of agent characters are exact repeats, and the top phrases are canned lines the disk cache in `tts_sarvam.speak_cached` already serves. Ceiling is ~₹0.15/min before dedupe, less after. |
| Redis | Nothing needs it. There is no Redis in this deployment (Postgres + a disk cache), and the only genuinely cacheable artefact is the provider-side prompt prefix, which lives at the provider. |
| Explicit `cachedContents` integration | The compat layer cannot reference a cache, and the tier returns `limit=0`, so the code path could not be tested. Untestable code on the live call path is worse than no code. |

### 27.6 What shipped

| File | Change |
|---|---|
| `shared/ai_pricing.py` | NEW. Central LLM rate card (USD/1M in · cached_in · out), `AI_PRICING_FILE`/`AI_PRICING_OVERRIDES`, USD→INR. Re-exports the Sarvam card so one import prices a whole call. Unpriced model → cost `None`, never `0.0`. |
| `app/voice/prompt_cache.py` | NEW. `split_prompt()`, `prefix_fingerprint()`, `estimate_tokens()`, and `probe_cache_capability()` — which turns the free-tier finding into one startup log line instead of a silent leak. |
| `app/voice/credit_tracker.py` | `record_llm()` + per-call/session LLM accumulators; `llm_event` rows in the same JSONL ledger as speech. |
| `app/voice/conversation.py` | `stream_options={"include_usage": True}` on the streaming turn; usage captured and recorded; capability probe fired from `warmup()`. |
| `conversation_call_bridge.py` | Passes the existing `call_id_provider` into the router so tokens and speech land on one ledger row. |
| `credit_report.py` | LLM section: requests, models, in/out tokens, **prefix cache hit-rate**, cost, and a FLOOR-ONLY warning when a model is unpriced. |
| `tests/test_prompt_cache.py` | NEW, 35 tests. Prefix stability per fixture group, provider-minimum check, fingerprint semantics, tier-variant mispricing, unpriced≠free, tracker accumulation. |

### 27.7 Two traps this cost, for whoever touches it next

**The usage chunk is provider-shaped.** OpenAI appends a final chunk with an
EMPTY `choices` list carrying `usage`; **Gemini's compat layer hangs `usage`
off a chunk that also has content.** Reading it only from empty-choice chunks
recorded zero requests against a live Gemini call. Both shapes are handled —
and the empty-`choices` guard is mandatory regardless: unguarded,
`chunk.choices[0]` raises IndexError, the outer handler swallows it, and the
caller hears the fallback line instead of the reply on *every* turn.

**Tier suffixes are not release suffixes.** `gemini-3.5-flash-lite`
`.startswith("gemini-3.5-flash")` is a naming coincidence; pricing it by naive
prefix match overstated it 6×. `llm_rate()` matches segment-wise and rejects a
match when an extra segment names a tier (`lite`, `mini`, `pro`, …).

### 27.8 Feature flags

    ENABLE_LLM_USAGE_TELEMETRY=true    # stream_options + ledger writes
    ENABLE_PROMPT_CACHE_PROBE=true     # startup capability probe
    PROMPT_CACHE_MIN_TOKENS=1024       # provider prefix floor used by the CI guard
    AI_PRICING_FILE= / AI_PRICING_OVERRIDES=   # rate card without a code change
    AI_USD_INR=88.33                   # matches WHISPER_PRICE_PER_MIN_INR

Telemetry fails open everywhere: a provider that rejects `stream_options` gets
one retry without it and the flag flips off for the process (it must never
demote the turn to the fallback model); a tracker that cannot import is a
no-op; the probe is a detached task that cannot delay a call.

### 27.9 LIVE PROOF (2026-08-11, `docker restart lescall-runtime`)

Deployed and verified with three full simulated calls through the real
pipeline (`callsim`, real Sarvam + real LLM). What the live runs established:

**The capability probe fires in production**, one line per process start:

    [PROMPT-CACHE] gemini/gemini-3.5-flash: provider caching UNAVAILABLE —
    free tier, cache quota is zero (…limit=0…) — enable billing on the Google
    Cloud project. The static prompt prefix is re-billed fresh on every turn.

**The cache-ordered prompt WORKS — measured, on a live call.** The primary's
free-tier day quota was spent, so every turn fell back to `gpt-4o-mini`, whose
automatic prompt caching *is* available. The ledger rows it wrote:

    gpt-4o-mini in=3282 cached=1920 (58.5%) out=34  prefix=c09bdeae92b5
    gpt-4o-mini in=3317 cached=1920 (57.9%) out=75  prefix=174f46c14bfb
    gpt-4o-mini in=3330 cached=3200 (96.1%) out=62  prefix=174f46c14bfb
    …
    day total: 22,144 of 30,376 input tokens — 72.9% served from cache

**96% prefix cache on a real turn.** The 27.4 projection is no longer a
projection: give this prompt a provider that offers caching and it hits. The
only thing between the platform and that saving on Gemini is the free tier.

Note the stable `prefix=` hash across turns — that is the fingerprint doing its
job. It changes once early (language lock re-renders the header) and then holds.

### 27.10 The free tier's real cap is 20 requests per DAY

The 5/minute limit in 27.3 is not the binding one. The live call surfaced:

    quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
    quotaValue: '20'   model: gemini-3.5-flash

**20 requests per day per model.** An average call is 8.8 turns, so the
configured voice model is exhausted after roughly **two calls a day**, after
which every turn silently falls back to `gpt-4o-mini` — a different model, a
different persona-following ability, and a different bill. This is invisible
without §27's telemetry: the fallback is logged per turn but nothing
aggregated it, and `calls.llm_models_used` only records that it happened, not
how often it dominated. Enabling billing fixes the cap and the caching in one
action.

### 27.11 Bug this deploy caught, and the guard added

The first live call recorded ZERO tokens despite making 7 LLM requests. Cause:
the retry-without-`stream_options` path treated **any** primary failure as
"provider rejected the parameter" and latched `_llm_usage_supported = False`
for the process. The first 429 (day quota) therefore disabled cost telemetry
permanently — a telemetry feature defeated by the exact condition it exists to
measure.

`_is_param_rejection()` now narrows it to a 400/422 whose message actually
names `stream_options`/`include_usage`. Quota, timeout, auth and 5xx take the
normal fallback path with telemetry left armed. Pinned by
`test_quota_error_is_not_mistaken_for_a_parameter_rejection`.

The fallback provider now also sends `stream_options`: with the primary's day
quota spent it serves every turn, and leaving it unmetered would leave the real
bill unmeasured exactly when it matters.

Separately, `name_confirm_block` was rendering ABOVE the ━━ CALL STATE ━━ cache
boundary — empty in every golden fixture, so nothing caught it, but on a live
call the turn after a caller gives their name it broke its own prefix. Moved
below the marker (fixture-neutral: the goldens are byte-identical). Pinned by
`test_pending_name_does_not_break_the_static_prefix`, which drives a real
router because only live state exposes this class of bug.

---

## 28. Document RAG hardening + measurable evaluation (2026-08-12) — AUDIT + SHIPPED

### What the document RAG was

`calling-agent/backend/app/documents/` grounds live calls in tenant-uploaded
documents. Before this work the whole pipeline was:

```
caller turn -> one embedding HTTP call -> cosine vs every chunk
            -> keep score >= 0.25 -> take 3 -> paste into the prompt
```

No lexical arm, no re-ranking, no citations, no cost accounting, no evaluation,
and — the load-bearing detail — **no record of how the index was built**.

### Four silent failures it had

1. **A model change made every stored vector meaningless.** `_EMBED_MODEL` was
   a module constant, the `.npy` recorded nothing, and the query-time cosine
   either raised a shape error or produced noise. Both were swallowed by
   `except Exception: logger.debug(...)`. The agent just quietly stopped using
   its documents. Now every index carries provider/model/dim/chunk-geometry and
   a content hash (`index_meta.json`); a mismatch is a named condition an
   operator sees in `GET /api/agents/{name}/documents` → `index.needs_reindex`.
2. **A failed embed at upload was permanent and invisible.** Now reported per
   document, repairable with `POST /api/agents/{name}/documents/reindex`, and
   the document stays reachable through the lexical arm meanwhile.
3. **The embedding HTTP call ran inline in the async turn** — measured p50
   320ms / p95 622ms of frozen event loop, taking VAD, STT and TTS with it. Now
   `retrieve_async` runs it in a worker thread with a deadline.
4. **Every turn retrieved, including "hmm".** Fillers now short-circuit before
   the API call: on 300 replayed real caller turns, 97 never reach it.

### What shipped

Hybrid retrieval (BM25 + vector, fused with RRF — the same fusion
`shared/intelligence/search.py` already uses), a conservative abstention gate,
structured citations, a two-tier semantic cache, per-retrieval tracing, and
embedding cost in the existing AI ledger (`credit_tracker.record_embedding_sync`
→ `shared/ai_pricing.embedding_cost`).

Measured on 37 golden questions over a 10-document corpus copied from live
platform content (`rag_eval/`):

| | before | after |
|---|---|---|
| Recall@1 | 0.66 | 0.79 |
| Recall@3 | 0.98 | 0.98 |
| MRR | 0.83 | 0.89 |
| NDCG@5 | 0.84 | 0.90 |
| End-to-end abstention (no-answer questions) | 0.56 | **1.00** |
| Over-abstention (answerable refused) | 0.00 | 0.00 |
| Faithfulness (LLM-judged) | 0.97 | 1.00 |
| Citation accuracy | n/a — none existed | 1.00 |

### Two defaults that are the opposite of the textbook

Both were measured, and both comments in `app/documents/config.py` carry the
table that decided them.

* **MMR is OFF** (`RAG_MMR_LAMBDA=1.0`). Diversity assumes the top hits are
  near-duplicates competing for one slot; here they are often joint evidence,
  because chunking overlaps by 60 words. MMR at 0.75 was worse at all 12 grid
  points — Recall@3 1.00 → 0.80 at the shipped chunking.
* **The similarity threshold stays at 0.25** and is NOT the abstention
  mechanism. Answerable and unanswerable questions overlap almost completely in
  cosine (0.26–0.74 vs 0.27–0.71: "how many employees does DhronAI have?"
  scores 0.71 because it is *about* the document's subject). Raising the floor
  to 0.40 catches 2 of 7 unanswerable questions and wrongly refuses 15 of 28
  answerable ones. Semantic absence is caught at generation time by the
  grounding contract (`app/documents/grounding.py`) instead, and the two layers
  are measured separately.

### Operating it

```bash
cd calling-agent/backend
../venv/bin/python -m rag_eval.runner                 # offline, free, deterministic
../venv/bin/python -m rag_eval.runner --fresh         # real API — the only real latency
../venv/bin/python -m rag_eval.runner --generate --judge --gen-provider openai
../venv/bin/python -m rag_eval.replay --limit 300 --live   # real caller turns
../venv/bin/python -m rag_eval.sweep --live           # parameter grid
../venv/bin/python -m pytest tests/test_rag_*.py      # 42 tests, the CI gate
```

`GET /api/agents/{slug}/rag/metrics` is the live view (latency percentiles,
abstention, cache, index health). `POST /api/agents/{slug}/rag/probe` with
`{"query": "..."}` answers "did retrieval miss, or did the model ignore the
evidence?" without placing a call.

### Migration — documents indexed before today

They keep working (the loader accepts an unlabelled index whose dimension
matches) and are reported as `unlabelled` / `needs_reindex`. Chunking also moved
400 → 250 words, which only applies to documents that are re-chunked. To adopt
both, per agent:

```bash
curl -X POST https://agent.lescall.com/api/agents/<slug>/documents/reindex
```

Re-embedding spends real tokens (~₹0.0000141 per 8-token query at
text-embedding-3-small), so nothing re-indexes itself implicitly.

### 28b. Follow-ups from a second-opinion review (2026-08-12)

The same brief was run independently on another machine; comparing the two
implementations surfaced three real gaps in this one. All three are fixed above
their measurements:

**Embedding spend was recorded but never reported.** `credit_tracker` wrote
`embed_event` to the ledger from day one, but `credit_report.py` only knew
`stt_event` / `tts_event` / `llm_event`, so the cost existed in the log and
nowhere a human looks. It now prints requests, tokens, stage split (query vs
ingest) and cost — at 6 decimals, because a query embedding costs ~₹0.00002 and
4 decimals renders a measured cost as ₹0.0000.

**Citations never reached the call record.** `_rag_citations` was assigned on
the router and read by nothing — the claim "citations are structured data on the
turn record" was true of the design and false of the code. Migration 0018 adds
`call_turns.rag_sources` (JSONB) + `rag_trace_id`; the pipeline attaches them
after generation and the bridge persists them. "The agent quoted a price on
Tuesday, where did that come from?" is now a SQL query against the call record
rather than a grep of a rotating trace file.

**Cross-document contamination was unmeasured.** Precision@K blurs two very
different failures: a chunk of the RIGHT document that is not the answering
passage (harmless padding) and a chunk of the WRONG document (a confident
irrelevant fact). Split out, the average is 0.11 legacy → 0.05 now, but the
average is the wrong number to quote — it is ~0.00 everywhere except
`wrong_context` (0.33) and `multi_hop` (0.10). A relative quality floor
(`RAG_RELATIVE_FLOOR=0.6`, swept) removes the case that motivated it: a caller
asking for a 3BHK in Surat under ₹1.1 crore was being handed the property list,
a rental listing for another city, AND a Nintendo console catalogue. Quality is
untouched at 0.6 and pays real recall past 0.7 — the table is in config.py.

Not adopted: the other implementation reports contamination 0.50 → 0.00 on its
own trap set. Ours starts at 0.11 and lands at 0.05 on this corpus, and the
residual is two documents about the SAME company (a company profile and a
staff-introduction blurb) ranking alongside each other — cross-document by the
metric's definition, benign in a caller's ears. Different corpus, different
number; the mechanism is the same and the measurement here is what shipped.

**Exact-term questions could be refused by their own document.** Found while
probing the live `dhron_calling` agent after the re-index: "what are the six
stages?" retrieved nothing. The chunk that literally contains "Six Stage
Implementation Approach" scores 0.222 — under the absolute floor — because
embeddings under-score short keyword questions, exactly the case the BM25 arm
exists to cover. The pruner then discarded it on the cosine alone, so the
lexical arm could find the answer and never deliver it.

The fix is a lexical FALLBACK: when the vector arm yields nothing above the
floor, the top BM25 hits are admitted instead. Fallback and not addition,
because admitting them alongside working vector results measured Precision@3
0.53 -> 0.46 and contamination 0.05 -> 0.17 — one shared common word is enough
to drag in another tenant's document. As a fallback it changes nothing on the
golden set and rescues the live query. `RetrievedChunk.kept_by` records which
arm admitted each chunk, and it is in the trace.

Worth noting for whoever tunes this next: it was found by probing a REAL agent
after deploying, not by the golden set, whose phrasing of the same question
scores above the floor. Probe production after a retrieval change.

**The tests were writing fake usage into the production cost ledger.** Caught by
reading the patched cost report on real data: it showed 3,457 embedding requests
against a model called `fake-embed-v1`. The RAG pipeline reports embedding
tokens through the credit-tracker singleton, and the unit tests — which embed
constantly with a fake provider — were appending to `credit_log/<date>.jsonl`,
the file `credit_report.py` reads. A cost ledger containing fabricated usage is
worse than one missing the feature entirely.

Closed at both sources: an autouse fixture in `tests/conftest.py` repoints the
tracker's log directory (resolved at import, so the module attribute is patched,
not the env var), and the evaluation harness disables cost tracking for its own
runs — an evaluation's spend is real but it is not a call, and it is already
reported by the eval itself. The 3,907 synthetic rows were removed from
2026-08-12; real STT/TTS/LLM events were untouched.

**Test-suite gotcha, for whoever chases this next:** the Postgres fixture
(`tests/test_intel_queue.pg_test_db`, session-scoped, shared by 10 modules)
rebuilds ONE database — `lescall_test` — with `DROP SCHEMA public CASCADE`.
Two pytest processes against the same host therefore destroy each other's
schema mid-run, and the symptom is a scatter of `UndefinedTable: relation
"phone_numbers" does not exist` in whichever modules happened to be running.
Observed while validating this work: the same commit produced 0, 3, 5 and 41
errors depending on what else was running, and a clean serial run is green.
Run one suite at a time, or give the second one its own database.

**And the reason it happened twice:** `credit_tracker._DEFAULT_LOG_DIR` was the
hardcoded string `/home/angelo/lescall/credit_log`, so ANY second checkout on
this host — a git worktree, a clone, a CI job — filed its test usage into the
production ledger no matter which tree it ran from. It is now resolved from the
module's own location, which is byte-identical in production and in the
container, and correct everywhere else. `SARVAM_CREDIT_LOG_DIR` still overrides.

---

## 29. SNR guard deafness — the caller's voice counted as noise (2026-08-17) — ROOT-CAUSED + FIXED

Reported as "the agent takes far too long to process the hellos — it waits for
complete silence before starting the script". It was not waiting and it was not
slow. It could not hear.

### 29.1 The call

`ffbdc517-dd6c-4048-8527-460fd92de45c` (outbound, +91 82084 30327, 11:46:35,
17.1s). Session summary:

```
first_transcript      15.159s
segments_created      2      segments_sent_to_stt   2
dropped_no_real_speech 5
```

The caller said "hello" five times. Four were discarded before STT. When the
fifth finally landed, the engine answered in **1 millisecond** — the connect
fast path had had its line ready the whole time:

```
11:46:50.192 [STT] Primary succeeded: 'hello'
11:46:50.194 [REPAIR] Connect hello — instant intro: 'Am I speaking to ...'
11:46:50.194 [ENGINE] 'hello' -> '...' (1ms, first_chunk=1ms)
```

`recordings/918208430327_20260817_114635/caller_audio.wav` is byte-exact the
stream the VAD consumed (859 frames x 320 samples == the file length), so the
audio is measurable. Every discard was audio hundreds of times above its own
line floor:

| caller speech | peak frame RMS | verdict |
|---|---|---|
| t=3.84–4.20s | 6320 | `snr=1.1x` **discarded** |
| t=9.21–9.30s | 4716 | `snr=0.0x` **discarded** |
| t=11.46–11.74s | 1849 | `snr=0.8x` **discarded** |
| t=12.36–12.62s | 513 | `snr=0.1x` **discarded** |
| t=14.10–14.64s | 10365 | `snr=60.7x` → heard, answered |

### 29.2 Why the ratio came out backwards

Three things compound, and only all three together produce the failure:

1. **The line is silence-suppressed (DTX).** Between utterances the caller
   channel is a *constant sample value* — zero variance, `|x|=8`. 42% of this
   call's frames were flat; `_track_media_liveness` logged it twice
   (`[MEDIA] caller channel recovered after 1.8s flat`). There is no ambient
   hiss, and speech arrives as an abrupt 300-400ms burst with no ramp.

2. **`FsmnVadStack.is_speech()` does not classify the frame it is given.** It
   returns `_speech_open`, state written by a worker thread consuming 100ms
   hops through a queue. Replaying the recording through the live stack
   measures the label window **200–280ms late**, and `FSMN_MAX_END_SIL_MS=280`
   holds it open past the end on top of that:

   ```
   real burst 1.02→1.38   model said 1.30→1.70   (+280ms)
   real burst 3.83→4.19   model said 4.10→4.50   (+270ms)
   real burst 9.21→9.29   model said 9.40→9.60   (ZERO overlap)
   ```

   For an utterance under ~400ms — i.e. every bare "hello" — the labelled
   window sits mostly on the silence AFTER the voice.

3. **`_segment_snr()` divided one by the other.** It accumulated
   `_speech_energy_sum` on `raw_speech` frames and `_noise_energy_sum` on the
   rest, resetting both at each flush. So the loud burst landed in the NOISE
   bucket and the DTX comfort fill landed in the SPEECH bucket. Reproduced
   exactly offline: `speech_mean=7.4  noise_mean=261  → 0.03x`, on audio whose
   true ratio was ~500x. `MIN_SPEECH_SNR=1.2` then discarded it as
   "no signal above line noise".

`SNR_ENFORCE_BELOW_MS=600` exempts only LONG segments, so a 300-400ms greeting
could never escape the energy judgement. **The failure was structural: on a DTX
line a bare "hello" was close to unhearable.**

### 29.3 Scale

Every discard on 2026-08-17 — **20 of 20, across 5 calls** — was this arm. Zero
`too short`, zero `no real speech`. Several carried 400-500ms of
model-confirmed speech.

```
f8e52713 (inbound, 13.3s)  4 dropped, 0 sent to STT, first_transcript —
1827485d (inbound,  9.1s)  2 dropped, 0 sent to STT, first_transcript —
ffbdc517 (outbound,17.1s)  5 dropped,                first_transcript 15.159s
```

Two callers were never heard at all. Such calls are then filed
`answer_state=empty reason=caller_silent`, so the lead reads as dead. Flat-frame
share tracks it exactly: the deaf calls are the DTX-heavy ones (42%, 34%).

### 29.4 What changed

**The guard no longer consults a label.** Both terms come from a rolling window
of raw frame energy (`_energy_window`, 5s):

- **floor** — `_ambient_energy()`, the `SNR_AMBIENT_PCTL`th percentile of recent
  frame energy. A burst is a small slice of any multi-second window, so the
  estimate cannot be dragged up by the speech it measures. Rolling and
  call-level: resetting it per segment is what let one burst dominate it.
- **speech** — `_segment_speech_energy()`, the mean of the loudest
  `SNR_SPEECH_TOP_FRAMES` (5 = 100ms) frames inside the segment. Peak-ish, so a
  late label window full of comfort noise cannot drag it to the floor; still an
  average over 100ms, so one click cannot fake it. The slice is the last
  `_speech_duration_ms / 20` window entries — that value advances by exactly
  20ms per frame appended to `_audio_buffer`, so it IS the segment's frame
  count — capped by `_frames_since_segment` so it can never reach past the
  previous flush.
- **absolute backstop** — `_segment_is_loud()`: past
  `barge_in_threshold * SNR_LOUD_EXEMPT_FACTOR` a segment is never
  energy-judged at all, whatever the floor estimate says.

**The connect intro now fires on speech ONSET, not on a transcript.**
`_maybe_connect_onset()` runs on the VAD frame path: `CONNECT_ONSET_MS` (140ms)
of model-confirmed speech inside `CONNECT_WINDOW_S` speaks
`conversation.connect_onset_line()` immediately — no endpoint wait, no STT
round trip, nothing that can silently drop the turn. The segment still goes to
STT normally. `note_connect_intro_spoken()` records it as if `process_stream`
had produced it and arms `CONNECT_ONSET_ABSORB_S`, during which bare hellos are
absorbed — so one caller utterance never earns two agent lines, and the repeats
behind it do not restart anything. Per-agent opt-out is unchanged
(`call_flow.connect.hello_fastpath`).

**Dead-air repair is no longer blinded by audio we threw away.**
`_silence_anchor()` read `_last_voice_activity`, which is bumped by every raw
VAD frame *including* discarded ones — so the pipeline believed the caller was
talking for 14 straight seconds while discarding every word, and the 6s
reprompt never came due. Repair now anchors on `_last_heard_activity`, set only
when a segment actually reaches STT. The raw signal stays where it belongs
(barge-in, echo, merge timing). Speech genuinely in progress still cannot trip
a reprompt — `_pipeline_quiet()` gates on that independently.

**The failure is now visible.** The discard log carries both measured terms
(`snr=0.2x (speech=1638 floor=6)` — the inversion is unmissable), two
consecutive SNR drops carrying real speech raise a warning while the call is
live, and `dropped_as_line_noise` is counted separately in the session summary.

### 29.5 Measured

Replaying all ten 2026-08-17 recordings through the real `_process_vad_frame`
and the real FSMN stack:

| | segments to STT | discarded |
|---|---|---|
| before | 34 | **14** (all this arm) |
| after | **48** | **0** |

`ffbdc517` alone: 3 sent / 4 discarded → **7 sent / 0 discarded**, the first
caller utterance now scoring 767x instead of 1.1x. The inbound call that was
never heard at all sends 4 segments at 768x–2785x.

End-to-end via callsim (`scenarios/hello_loop.yaml`, four turns opening with
three hellos):

```
[CONNECT] Onset intro at 140ms of speech (2.2s into call) — not waiting for a transcript
[CONNECT] Absorbing bare hello 'hello?' — already answered on onset
double_replies=0
```

**2.2s to the second beat, against 15.159s on the live call.** Dead-air repair
re-verified on `scenarios/silence_reask.yaml`:
`[REPAIR] Dead air 6.5s — reprompt 1/2`.

### 29.6 Regression gate

`tests/test_vad_real_speech_floor.py` gained a `_LaggingVad` whose labels lag
its own energy the way FSMN's do, plus `_old_style_snr()` — the pre-fix
estimator, kept in the suite deliberately. `test_the_defect_is_real_the_old_
estimator_inverts` asserts that estimator still comes out below the reject
floor on the synthetic: **if that assertion ever stops holding, the regression
test next to it has quietly stopped exercising the bug.** All the original
rejection tests are unchanged and still pass — the 80ms "₹1 lakh." blip, the
RMS-15 flat line, quiet-caller-above-their-own-floor — because they were
written with labels aligned to the audio, which is exactly the case this change
does not touch.

Caveat worth keeping: on the 2026-08-17 corpus the guard now rejects **nothing**
(all 14 drops were false). Its protective behaviour is pinned only by the
synthetic tests — that corpus contains no known hallucination case.

### 29.7 Knobs

```
SNR_AMBIENT_WINDOW_FRAMES=250   # 5s rolling floor estimate
SNR_AMBIENT_PCTL=10             # percentile taken as the line floor
SNR_SPEECH_TOP_FRAMES=5         # loudest 100ms of the segment = speech term
SNR_LOUD_EXEMPT_FACTOR=1.0      # x barge_in_threshold; 0 disables the backstop
SNR_DROP_STREAK_WARN=2          # consecutive real-speech SNR drops before warning
CONNECT_ONSET_TRIGGER=true      # telephony default; onset-fired connect intro
CONNECT_ONSET_MS=140            # speech before it fires
CONNECT_ONSET_ABSORB_S=8.0      # window in which the triggering hello is absorbed
```

### 29.8 Still open — a SECOND, different deafness

`cd2d33cf` (inbound, +91 83695 59482, 23.1s) was never heard either, and this
change does **not** fix it. Its caller channel peaks at RMS 6294 with 70 frames
above RMS 200, but FSMN never opened once:

```
FSMN speech runs: NONE
webrtc speech frames: 765 / 1156
```

Nothing reached the SNR guard — no segment was ever created. That is the model
itself failing on that audio (or that audio not being speech: spectral flatness
0.39 is noise-like, above the 0.1-0.3 speech band). Separate investigation;
the DB already files it `answer_state=empty reason=no_caller_speech`.

### 29.9 Unrelated trip hazard found while validating

`calling-agent/.env` has `SARVAM_API_KEY=sk_uf9gu0sz_…Jcf6`, which returns
**402 `insufficient_quota_error`** on both TTS endpoints. Production is
unaffected — `key_rotator` leases a per-call key from the 11-key pool and all
11 answer 200 — but anything reading `SARVAM_API_KEY` directly fails, which is
how callsim's caller synthesis dies (`./callsim` re-sources `.env` with
`set -a`, so exporting a good key before it does nothing). Worth swapping for a
live key.

---

## 30. Public integration API — `/v1` (2026-08-19) — SHIPPED, LIVE

A customer's CRM or ERP can now push contacts into LesCall and pull every
call's intelligence summary, transcript and recording back out, with a key the
customer mints themselves and a source-IP boundary an operator sets.

Everything below is live on `https://agent.lescall.com/v1`.

### 30.1 Why it is not under `/api/`

`auth.install_login_gate` is a deny-by-default allowlist of `/api/` prefixes
built for *session* auth: a prefix added there becomes reachable for every
logged-in session, and every future sibling route inherits the exemption by
prefix. A key-authenticated surface underneath it would have to be granted such
a bypass. `/v1` is a separate namespace with a separate credential, listed once
in `_OPEN_PREFIXES`, and it enforces tenancy, IP policy and rate limits itself.

### 30.2 The parts

| File | Job |
|---|---|
| `shared/db/migrations/0019_public_api.sql` | `api_keys`, `api_access_policy`, `api_rate_windows`, `api_idempotency` |
| `shared/public_api/keys.py` | token format, hashing, **the whole admission chain** |
| `shared/public_api/policy.py` | per-tenant policy, CIDR matching, limit precedence |
| `shared/public_api/ratelimit.py` | fixed-window counters (in PG, not memory) |
| `shared/public_api/idempotency.py` | replay protection for writes |
| `shared/public_api/sanitize.py` | boundary scrubbing (NUL bytes, control chars, width/depth caps) |
| `shared/public_api/serialize.py` | row → the documented JSON shapes |
| `shared/contacts.py` | row-level contact writes shared with the dashboard |
| `dashboard/public_api.py` | the `/v1` routes; `install(app)` also registers the error handler |
| `dashboard/public_api_spec.py` | the hand-authored OpenAPI document |
| `dashboard/api_keys_api.py` | `/api/apikeys` — Settings → API access |
| `dashboard/admin_api_console.py` | `/admin/integration` — the operator's controls |
| `frontend/src/components/ApiAccessPanel.tsx` | the key-generation UI |
| `frontend/src/components/ApiDocumentation.tsx` | the reference, shown in Settings and at /api-docs |
| `frontend/src/pages/ApiDocs.tsx` | the standalone, printable page |
| `calling-agent/backend/tests/test_public_api.py` | 38 functional tests, real ASGI client |
| `calling-agent/backend/tests/test_public_api_security.py` | 56 adversarial tests (§30.11) |

### 30.3 Credentials

Token shape `lk_live_<key_id>_<secret>`. `key_id` is 16 hex chars and public —
it travels in the token so verification is one indexed lookup rather than a
scan. `secret` is 256 bits; **only its SHA-256 is stored**, so a database dump
cannot be replayed. Plain SHA-256 (not bcrypt) is deliberate: the secret is
random, not a password, so there is no dictionary to run and no work factor
worth paying per request — the same reasoning as `calls.download_token`.

`Authorization: Bearer …` is documented; `X-API-Key` is accepted because many
CRM HTTP connectors cannot set an Authorization header, and the alternative is
customers putting the key in a query string where every access log keeps it.

### 30.4 The admission chain (`keys.authenticate`)

Order is load-bearing:

1. parse the token → `key_id`
2. look up the row, `compare_digest` the hash
3. **the account's IP allowlist** — *before* status and expiry, so an unlisted
   address never learns whether the key it presented was real, revoked or
   expired
4. status → expiry → account enabled
5. the key's own (narrowing-only) allowlist
6. rate limit; the counter is incremented *before* the verdict, so a client
   that keeps hammering after a 429 stays pinned

Client IP comes from `X-Real-IP` **only when the peer is loopback** (i.e. our
nginx, which overwrites that header). A direct hit on `:8080` uses the socket
peer. Without that rule the allowlist would be decorative — anyone could claim
to be an allowlisted address. `test_forged_forwarding_headers_are_ignored_from_untrusted_peer` pins it.

### 30.5 Rate limits and the IP rules (the admin page)

`/admin/integration` lists every account; `/admin/integration/<owner>` is the
editor. The allowlist is typed one rule per line:

```
203.0.113.9/32 | Pramukh CRM prod | 300 | 100000
10.20.0.0/16   | office VPN
```

`cidr | label | rate_per_min | rate_per_day`. **Empty list = any IP.** Non-empty
= every other address is refused. A per-line rate **raises** the ceiling for
that one machine; the most specific match wins, so a `/32` beats the `/16` it
sits inside. A per-KEY limit can only lower whatever it lands on.

A bad line refuses the WHOLE save. Dropping it silently would leave an operator
believing they had restricted an account that is still wide open.

Defaults for an account with no row: enabled, any IP, 60/min, 20 000/day, 5 keys.

Counters live in `api_rate_windows` in Postgres, not process memory, so
`docker restart lescall-api` does not hand every caller a fresh budget, and the
number an operator sees is the number the limiter enforced. The limiter **fails
open** on a DB error — an outage for every integrated CRM is worse than briefly
unmetered traffic, and nginx's `limit_req zone=lescall_api` (20 r/s, burst 40,
per source IP) is still underneath as a floor for floods of *invalid* keys,
which never reach a per-key counter.

### 30.6 Reading calls

`GET /v1/calls?order=asc&cursor=<last id imported>` is THE sync loop. Keyset
pagination on the call's own id cannot skip or duplicate a row when a call
lands mid-import, which offset pagination does routinely.

`GET /v1/calls/{call_id}` is the full export: metadata, the `call_intelligence`
record with its JSON columns parsed, commitments, action items, follow-up,
`collected_data`, the transcript as turns, and the recording. `call_id` is
`call_key` — a stable string a CRM stores forever, never the integer PK.

Scopes are checked **per section**: a key without `transcripts:read` still gets
the call and its scores, with `omitted_sections` naming what was left out.
Refusing the whole record would make narrow keys useless.

Analysis is asynchronous — a just-ended call returns `analyzed: false`, which
means come back, not failure.

Recordings stream through `dashboard/recording_response.py`, the same code the
dashboard plays (extracted from `server.py` for exactly that reason). The call
record also carries the existing credential-free tokenized `download_url`.

### 30.7 Writing contacts

Upsert by the last 10 significant digits, matching `idx_contacts_owner_sig` —
"+919876543210", "09876543210" and "9876543210" are one person. Only fields the
caller SENT are written, so a partial CRM payload cannot blank an operator's
notes; `fields` merges. A different `phone` on PATCH **renumbers** the row
(keeping its calls) rather than forking a second person, and 409s if that
number is already someone else's.

`POST /v1/contacts/bulk` takes 500 and is always 200 with a per-row result — a
400-row import with three bad numbers must land 397 leads, not make the caller
re-send everything.

`Idempotency-Key` replays the stored response. Reusing a key with a *different*
body is a 409, because otherwise a client bug silently returns the wrong
contact's id.

### 30.8 Deploying / verifying

```bash
# 1. schema (additive; four new tables)
set -a && . deploy/.pg_env && set +a
calling-agent/venv/bin/python -m shared.db.migrate

# 2. code
docker restart lescall-api            # /v1 + /api/apikeys
docker restart lescall-admin          # /admin/integration
docker compose -f deploy/docker-compose.services.yml up -d --build spa

# 3. nginx: the /v1 location + its limit_req zone
sudo cp deploy/nginx-agent.lescall.conf /etc/nginx/sites-available/agent.lescall
#   ^ then STRIP the trailing `default_server` block — it is installed
#     separately as 000-lescall-default and nginx refuses the duplicate.
sudo nginx -t && sudo systemctl reload nginx

# 4. verify
curl -s https://agent.lescall.com/v1 | head -c 120         # service descriptor
curl -si https://agent.lescall.com/v1/me | head -3         # 401 + error envelope
docker logs lescall-api --since 60s | grep "Public API"    # "mounted"
```

Boot lines to expect: `Public API (/v1) mounted` and `API key management
mounted`. If either is missing the router failed to import and the log carries
the traceback — customer integrations are down while it is absent.

### 30.9 Tests

```bash
cd calling-agent/backend && ../venv/bin/python -m pytest tests/test_public_api.py -q
```

38 functional tests (plus the 56 adversarial ones in §30.11), driven through a
real ASGI `TestClient` from 127.0.0.1 with a
per-request `X-Real-IP` — i.e. the production request path, not a stub. They
cover the token round trip and that the secret appears nowhere in the row, IP
refusal ordering, most-specific-rule matching, forged forwarding headers,
rate-limit headers and 429, scope gating, contact upsert/renumber/bulk,
idempotency replay and mismatch, keyset pagination stability, owner scoping
(404, never 403), and that every error uses one envelope.

### 30.10 Not built (deliberately)

* **A server-side PDF renderer.** See §30.12 — the browser's print pipeline
  produces a better document here than cairo/pango would, for no new
  dependencies on a box that carries live calls.
* **Webhooks.** Polling `?order=asc&cursor=` covers the integration case
  without a delivery queue, retry ladder, signing secret and dead-letter
  surface. `outbox`/`outbox_worker.py` is the existing push machinery if this
  is ever wanted.
* **Writes into the intelligence layer.** The API is read-only over calls on
  purpose: those rows are the AI's output, and a CRM overwriting them would
  make the analytics unfalsifiable.

### 30.13 The API is a GROWTH capability (2026-08-19)

Migration `0021_api_access_entitlement` adds `plans.api_access_enabled` and
`entitlements.api_access`, following the same shape as every other marketed
capability (0013 §Catalogue). Seeded **1 for `growth` and `enterprise`, 0 for
`free`, `basic` and `pilot`.**

Pilot is deliberately excluded: a 7-day trial that ends with a customer's CRM
silently losing its feed is a worse first experience than never wiring it up.

Enforced in TWO places, both via `feature_allowed(owner, "api_access")`:

* `keys.authenticate()` — the whole `/v1` surface, returning
  `402 plan_required`. Checked AFTER credential validity, so a customer with a
  typo'd key is told about the key, not sent to the billing page.
* `api_keys_api.create_api_key()` — minting a key raises `BillingRefusal`, so
  the SPA gets the standard `code`/`upgrade` pair and renders the upgrade panel.

**It is a column, not `plan_code == 'growth'`.** That is what lets the admin
plane grant the API to one Enterprise tenant, or to a customer mid-negotiation,
with an `api_access` override instead of a code change — see the banner on
`/admin/integration/<owner>`, which states the entitlement and says so.

**Today it is inert.** Platform billing enforcement is still OFF (RUNBOOK §25),
and `feature_allowed` returns True for everything while it is. The gate arms
itself the day enforcement is switched on; it needs no second deployment. The
Settings panel reads the same function, so the upgrade prompt appears then and
not before.

Existing keys are NOT revoked on a downgrade — they simply stop authenticating,
which is reversible the moment the plan is restored.

### 30.11 Adversarial audit (2026-08-19) — 9 findings, all fixed

`tests/test_public_api_security.py` (56 tests) was written to break the surface,
not to prove it works. What it found:

| # | Sev | Finding | Fix |
|---|---|---|---|
| 1 | **HIGH** | **The account rate ceiling was per-KEY.** A tenant allowed 60 r/min could mint five keys and take 300 — the number in the admin console described nothing anybody was held to. | Migration 0020: windows are keyed on a SUBJECT. Every request now counts against `key:<id>` **and** `acct:<owner>`. `policy.account_ceiling()` sets the account budget to the most generous rule anyone in the account was granted, so a deliberate per-IP raise is never cancelled by it. |
| 2 | MED | **Key-existence oracle.** "That API key does not exist" vs "is not valid" confirmed whether a key id was real — turning a truncated log line into a target. | One message for both. |
| 3 | MED | **NUL byte → 500.** `?q=%00`, or `\u0000` in a pushed name, reached Postgres (`text fields cannot contain NUL`) and 500'd. Any caller could trigger it at will. | `shared/public_api/sanitize.py`, applied at the two boundaries (`_json_body`, `_q`). Strips C0 controls, keeps tab/newline, caps width and depth. |
| 4 | MED | **404 / 405 / 500 escaped the error envelope** — an unknown path answered with FastAPI's `{"detail":…}` and an unhandled exception with bare `Internal Server Error`. | A `/v1`-scoped middleware plus a `RequestValidationError` handler. Everything under `/v1` is now one shape; non-`/v1` traffic is untouched. |
| 5 | MED | **Sustained abuse cost 6 statements per REFUSED request** (4 window upserts + policy read + key touch) on the box carrying live calls. | In-process refusal cache (30s TTL — capped well below a day window so an operator RAISING a limit is not ignored until midnight) + a 10s policy cache + no key-touch on a cached refusal. A hammering client now costs one indexed SELECT. |
| 6 | MED | **No brute-force throttle.** A scanner walking key ids got unlimited indexed lookups. | 20 failed auths per IP per minute, refused from memory. |
| 7 | LOW | **Junk `X-Real-IP` was stored** on the key row and rendered as an address in two UIs. | `client_ip()` validates with `ipaddress` and returns `""` — which never matches an allowlist, so a restriction still fails closed. |
| 8 | LOW | **Encoded slash escaped the `/v1` location.** nginx normalises before matching, so `/v1/calls/..%2f..%2fetc/x` was routed to the SPA fallback. Harmless in effect (the SPA container has nothing to leak) but the request went somewhere its author did not name. | nginx `map` + `return 400` on `%2F`/`%5C` **in the path only** — the query string is excluded so `?next=%2Fdashboard` keeps working. |
| 9 | LOW | **`max_keys` race** — two simultaneous creates both passed the cap check. | Count and insert share one transaction. |

Checked and found already sound, so do not "fix" them again: parameterised SQL
throughout (the f-string fragments interpolate only literals); 404-not-403 on
every cross-tenant read; scope gating per SECTION of the call record;
per-key idempotency records; `download_url` gated behind `recordings:read`;
`compare_digest` on the hash; SameSite=lax + no `allow_credentials` making
`/api/apikeys` CSRF-proof.

Run the audit:

```bash
cd calling-agent/backend
../venv/bin/python -m pytest tests/test_public_api.py tests/test_public_api_security.py -q
# 94 tests
```

### 30.14 Pilot gets the API too (2026-08-19)

Migration `0022` flips `plans.api_access_enabled` for `pilot`. 0021 had excluded
it; the owner's call is the opposite and is the stronger argument for a trial
specifically — a prospect evaluating LesCall against an incumbent has to SEE the
data land in their own CRM, not be told it would.

Expiry behaviour is unchanged and worth knowing: when the 7-day window elapses
the entitlement resolves back to Free, `api_access` returns to 0, and existing
keys start returning `402 plan_required`. **Keys are not revoked** — upgrading
restores them instantly. `test_pilot_can_use_the_api_and_loses_it_when_the_window_ends`
pins the whole cycle.

Current grants: `pilot`, `growth`, `enterprise`. Not `free`, not `basic`.

### 30.12 The in-product reference + PDF (2026-08-19, restructured)

**The integration surface is its own destination, not a Settings tab.** It was
one first, and that was wrong twice over: a customer's engineer had to go
through an *account* page to reach what they work in all day, and the reference
had nowhere to breathe, so it read as a wall of text inside a settings drawer.

* **`/developers`** (`pages/Developers.tsx`) — the hub. Opens with the two
  directions data actually moves (contacts in / calls + AI out), because that is
  the product's proposition and it is answerable with this account's live
  numbers rather than a marketing sentence. Then keys, start-here snippets,
  limits and usage. Sidebar entry under **Account**.
* **`/developers/reference`** (`pages/ApiReference.tsx`) — opens with a
  13-row endpoint index, because developers want the map before the prose.
  `/api-docs` redirects here; the old URL may be in a bookmark or an email.
* `frontend/src/components/ApiDocumentation.tsx` — the reference body. Takes
  the reader's OWN limits as props rather than quoting platform defaults, so a
  customer granted 300/min is not told 60.
* **Settings is now short.** Account profile plus two signposts (Developers,
  Billing). Its chunk went 22 kB → 8 kB.
* **The raw `/v1/openapi.json` link is gone from the product UI.** A naked file
  link in product chrome reads as unfinished. The endpoint still serves — the
  reference now offers it in a sentence, to teams that ask for it.
* **PDF is the browser's print pipeline**, driven by an `@media print` block in
  `index.css` scoped to `body.printing-doc` (set only while `/api-docs` is
  mounted, so printing any other panel is unaffected). It hides the app shell
  via the visibility trick, forces the light palette, and sets
  `break-inside: avoid` on code blocks, tables and endpoint cards.
  There is deliberately **no server-side renderer**: this host has no PDF
  engine and no headless browser, and adding weasyprint's cairo/pango stack to
  a 2-core box carrying live calls buys a worse document than the browser
  already produces.

---

## 31. The verified smart filter + Excel exports to GCS (2026-08-25)

### 31.1 The defect

The Intelligence page's natural-language box ran hybrid retrieval and showed
the top-K. Retrieval answers *"which calls are ABOUT this?"*. A filter asks
*"which calls SATISFY this?"* — and on this corpus the two questions have
almost the same embedding:

```
"whoever relocated to Vapi"              ─┐  cosine ≈ 0.95
"whoever said yes to relocate to Delhi"  ─┘
```

Every call that discussed relocating ranked high on both, so the Delhi query
returned the Vapi calls: plausible rows, wrong answer. The truthful answer to
the Delhi query on this account is **zero rows**, and no re-ranking of a
similarity score can produce a zero. Reported by the owner as "the filter
system has big flaws".

### 31.2 The fix: retrieve, then DECIDE

`shared/intelligence/verify.py`.

```
phrase ──► planner (1 call)  ──► requirement checklist + paraphrases + keywords
                                        │
retrieval arms (widened by the paraphrases) ──► up to 120 CANDIDATES
                                        │
                       judge, 1 call per candidate, 12 in flight
                                        │
     gate 1: every stated check true
     gate 2: confidence ≥ INTEL_FILTER_MIN_CONFIDENCE (0.55)
     gate 3: the quoted evidence really occurs in THAT call's transcript
                                        │
                                    matches only
```

**Recall is the judge's blind spot**, so the planner's paraphrases and
keywords each get their own retrieval arm — a transcript that said "shift"
where the user typed "relocate" is only reachable that way. Those arms share a
fixed voting BUDGET in the RRF fusion (`PROBE_BUDGET=1.0`,
`KEYWORD_BUDGET=0.5`, split across however many arrive) rather than a per-arm
weight: the model chooses how many keywords to emit, and at a flat 0.35 each,
six single-word arms ("yes", "agree", "moving") summed to 2.1 and out-voted
the question itself. A budget makes the ceiling a property of the code.

**Gate 3 is the load-bearing one.** It reuses `extract.quote_in_transcript`,
the same hallucination guard the extraction stage puts on customer quotes. A
model that wants to be helpful can assert a match; it cannot fabricate words
into a transcript we already hold. That is what makes an empty result
trustworthy rather than a shrug.

Model: **`gemini-3.5-flash-lite`**, the smallest on the key. This is one call
per candidate against a ~1.7 kB transcript — a task a bigger model does no
better, only slower and dearer. Measured ~1.3 s per judgement, so a cold
103-candidate filter completes in ~15 s wall clock and a repeat is instant
from the verdict cache.

### 31.3 Strict about entities and stance, NOT about tense

The first cut of the planner turned *"whoever relocated to Vapi"* into "the
caller has completed the move" and returned 0 — on a corpus where 3 callers
plainly said yes to relocating. A phone call captures what people **agree
to**, not what they later did.

So the rubric is:

* **entities** — Vapi ≠ Surat ≠ Delhi. 3BHK ≠ 2BHK. A sibling entity is a NO.
* **stance** — "yes, I'm fine with that" ≠ "no, I can't relocate right now".
  The agent *offering* is not the customer *accepting*.
* **tense** — read charitably. Completion is required only when the phrase
  says so ("already moved", "has shifted").

The planner prompt carries worked examples for exactly this case. Do not
"tighten" it back; re-read §31.3 first.

Measured on `angelo@dhronai.com` (2026-08-25, 396 calls):

| filter | candidates | matched |
|---|---|---|
| `whoever relocated to vapi` | 59 | **3** (504, 508, 516 — all said yes) |
| `who refused to relocate to vapi` | 92 | **2** (515, 132 — both declined) |
| `whoever said yes to relocate to delhi` | 97 | **0** ✅ |

Call 515 ("no, I don't think I can relocate right now") is correctly IN the
refusal filter and OUT of the relocation filter. That discrimination is the
whole feature.

### 31.4 What the UI must show

A verified filter changes what the page owes the user:

* the **requirements it checked**, verbatim — otherwise a precise empty result
  is indistinguishable from a broken search;
* **read N of M candidates · K matched**, so silent truncation is impossible;
* the **quoted line** on every row, in place of the summary — "why is this
  here?" is the question a filtered list has to answer;
* an empty state that says *"none of the N calls we read match — that is the
  answer, not a miss"*.

When the judge cannot run, `verified: false` comes back and the UI says
*"these are similarity matches, not checked ones"*. **Never a silent
fallback**: quietly serving the loose results is the bug this replaces.

### 31.5 Cost, the verdict cache, and why the PLAN is cached too

One planner call per question + one judge call per candidate. Verdicts land in
`intel_filter_verdicts` (migration 0023), keyed by

    question (sha256 of the normalised phrase | rubric version | model)
      × that call's CONTENT (sha256 of transcript + summary + intent)

so a re-analysed call invalidates only its own row, and bumping
`FILTER_RUBRIC_VERSION` retires every stale verdict at once instead of letting
it outlive the rubric that made it.

**The key deliberately does NOT include the planner's requirement text.** The
first cut did, and measured a **0% cache hit rate on a verbatim repeat of the
same filter**: the planner is an LLM, and asked the same phrase twice at
temperature 0 it still writes *"Calls where the caller agrees or confirms they
have relocated to Vapi"* one run and *"Calls where the caller confirms they
are relocating to Vapi"* the next. Different text → different hash → 100+
judgements paid for again on every page refresh.

Migration **0024** fixes it one level up: `intel_filter_plans` caches the
checklist per `(owner, question)`, so the same phrase always judges against
the same requirements and the verdict key can be the question itself. The
fallback checklist (used when the planner is DOWN) is deliberately never
cached — persisting it would freeze the degraded rubric in place.

Measured on the live account, 71-candidate filter:

| run | examined | cached | wall clock |
|---|---|---|---|
| cold | 71 | 0 | **12.0 s** |
| repeat | 71 | 71 | **1.4 s** |

Budget: `INTEL_FILTER_BUDGET_S` (60 s) is a hard wall-clock stop. On
exhaustion the pass reports `truncated: true` and how many it read — it does
**not** pad the tail with unjudged rows.

### 31.6 Excel exports → Google Cloud Storage

Ticked rows on Intelligence → **Export to Excel** → one workbook, one sheet
per question, uploaded to the tenant's GCS bucket.

| sheet | grain |
|---|---|
| Summary | what this file is + the question that produced it |
| **Numbers** | one row per PHONE — the de-duplicated dialling list, hottest first |
| Contacts | the phone-book record behind those numbers |
| Calls | one row per call, every score + `Open in LesCall` link |
| Insights | topics, objections, buying signals, budget, next actions |
| Transcripts | the words (truncated at 32 000 chars — Excel's cell ceiling) |
| Evidence | present when the selection came from a verified filter: the quoted line per call |
| Commitments | every promise and whether it is still open |
| Legend | what every band and score means |

`Numbers` is separate from `Calls` on purpose: a repeat caller is ONE line on
a dialling list, not four.

Storage doctrine (`shared/export_store.py`) — same as call recordings, §6b:

* **No signed URLs.** The org policy blocks SA key creation, so there is no
  private key to sign with. The bucket stays private and OUR 256-bit token is
  the credential: `/api/exports/dl/<token>`, allowlisted by prefix in
  `auth._OPEN_PREFIXES` (under a `/dl/` segment so it cannot widen to the
  owner-scoped routes beside it). Any miss returns an identical 404.
* Auth is the VM's **attached service account via metadata ADC**. Never set
  `GOOGLE_APPLICATION_CREDENTIALS`.
* Object: `exports/<owner>/<YYYY>/<MM>/<id>-<name>.xlsx` in
  `GCS_EXPORTS_BUCKET` (defaults to `GCS_RECORDINGS_BUCKET`).
* **A failed upload is not a failed export.** The workbook is written locally
  first — that copy is both the serving fallback and the retry source — and
  the row records `storage='local'` with the error. The user still gets their
  file; the UI says the cloud copy is missing rather than pretending.
* Delete revokes the link and hides the row but **leaves the GCS object**: the
  bucket is the customer's record of what was exported, and a dashboard click
  should not silently delete from their cloud storage.

### 31.6b Post-ship audit (2026-08-25) — 6 defects found and fixed

Re-read of the shipped code the same day. All six were silent failures; four
of them would have looked like the feature simply working.

1. **One-word queries silently skipped the AI judge.** `_looks_like_identifier`
   was "no spaces and 8+ characters", so `relocation`, `complaints`,
   `interested` and `possession` were read as pasted identifiers and bypassed
   verification entirely — falling straight back to the loose similarity
   matching this feature replaces, with `verified: false` buried in a field
   nobody reads. The predicate is now "carries digits", which is what every
   identifier on this platform actually has. Same bug in reverse: `+91 98765
   43210` has spaces, so a pasted phone number was sent to the judge and spent
   ~100 model calls confirming a primary key. `tests/test_intel_filter_api.py`.

2. **`verified: true` on a pass where the model never answered.** The flag was
   `bool(verdicts)`, but calls with no transcript are decided in CODE and put
   rows in there without the judge running. A pass in which every request
   failed could therefore present "0 matches" as a checked answer. Now it
   requires a real judgement (fresh or cached) or nothing to judge.

3. **`verified: false` on an honest empty retrieval.** The mirror: zero
   candidates meant no verification pass ran, so the UI warned "these are
   similarity matches" about a list it had never claimed anything about.

4. **Bucket drift.** `open_stream` resolved the bucket from the LIVE config
   while `call_exports` records `gcs_bucket` per row. §31.6 offers
   `GCS_EXPORTS_BUCKET` as a one-line switch — taking it would have 404'd
   every earlier export the moment its local copy aged out, months later, with
   nothing connecting the failure to the config change. Reads now use the
   bucket on the row.

5. **Retention was blind to bucket lifecycle rules.** `cleanup_local` deleted
   the local copy on the strength of `storage='gcs'`, which only records that
   the upload succeeded once. **This VM's service account has object-level
   access only** — `storage.buckets.get` is denied, so we cannot read the
   bucket's lifecycle configuration at all. An operator's delete-after-N-days
   rule on `lescall-recordings` is therefore invisible to us and would have
   silently destroyed the last copy. Cleanup now re-confirms the object each
   pass and keeps the file whenever it cannot (a bucket it cannot reach counts
   as cannot).

6. **`Numbers` reported one direction per phone.** Whichever call was seen
   first won, hiding half the story on the contacts that matter most — the
   ones we called who then called back. It aggregates now
   ("inbound, outbound").

Also: the criterion echoed onto the Summary sheet arrives from the browser and
is now bounded rather than trusted. It is still CLIENT-supplied on purpose —
the sheet must record the checklist the user actually saw, not one a re-plan
might have reworded.

### 31.6c Why exports share the recordings bucket

`lescall-exports` was considered and is **not** creatable from this host: the
VM's attached service account (`lescall-recsync@`) holds object-level rights
only, so both `storage.buckets.create` and `storage.buckets.get` return 403.
Exports therefore live under the `exports/` prefix of `lescall-recordings`,
which the same credential can already write.

If you want a dedicated bucket, it has to be created by a principal with
project-level storage admin — then grant `lescall-recsync@` objectAdmin on it
and set `GCS_EXPORTS_BUCKET`. Existing exports keep working across that switch
because every row records the bucket it was written to (defect 4 above).

**Open risk, un-auditable from here:** if a lifecycle rule on
`lescall-recordings` deletes objects after N days, it will delete exports too.
We cannot read that configuration with this credential. Defect 5's fix means
we notice and keep the local copy rather than losing the file, and the loss is
logged — but the durable copy would be gone. Worth checking in the Cloud
console once.

### 31.7 Endpoints

```
GET    /api/intel/search?q=…&verify=1     # verified filter (verify=0 = raw retrieval)
POST   /api/intel/exports                 # {call_ids|filters, title, query} -> workbook
GET    /api/intel/exports                 # recent exports for this account
GET    /api/intel/exports/{id}/download   # session-authenticated
DELETE /api/intel/exports/{id}            # revoke the link
GET    /api/exports/dl/{token}            # public tokenized (no session)
```

### 31.8 Deploying

```bash
python -m shared.db.migrate                                    # 0023 + 0024
cd frontend && npm run build
docker compose -f deploy/docker-compose.services.yml up -d --build spa
docker restart lescall-api                                     # dashboard/*.py
```

### 31.9 Tests

```
tests/test_intel_verify.py    19   the three gates, cache identity, degradation
tests/test_intel_export.py    15   sheet grain, tenant scoping, Excel cell limits,
                                   retention vs lifecycle rules, bucket drift
tests/test_intel_filter_api.py 20  which queries may skip the judge (§31.6b #1)
tests/test_intel_search.py    +6   multi-probe arms and the voting budget
```

The verify suite stubs the model deliberately — it tests the gates code owns,
not whether the model is clever. `test_fabricated_evidence_is_a_hard_no` is
the one that matters: a match whose quote is not in the transcript is dropped.

---

## 32. The smart filter showed half the answer (2026-08-29) — ROOT-CAUSED + FIXED

### 32.1 The report

> "I generated a report of people willing to relocate to Vapi and it showed 50.
> Then I searched people NOT willing and it showed 2. This was suspicious
> because I remember sending around 200 calls in the last campaign. Also I
> searched for people who know DevOps and are willing to relocate and it
> showed only a few."

Three suspicions. **One was a real defect and two were the data telling the
truth** — worth separating, because "fixing" the last two would have broken a
filter that was working.

### 32.2 The corpus

`angelo@dhronai.com`, 609 calls, 1481 embedding chunks. The campaign is
`shubhangi-outbound` + `shubhangi-inbound` — 211 calls, 2026-08-19 → 08-27, an
outbound recruiting screen for Dhron AI technical roles. The agent asks each
candidate whether they will relocate to Vapi, Gujarat for an on-site role.

Ground truth for this section was produced by running the SAME judge
(`verify.judge_call`, same planner criterion) over EVERY call in the account
with retrieval removed from the path. That is the number retrieval would return
if its recall were perfect, and it is the only honest yardstick — comparing a
filter against itself measures nothing.

### 32.3 What was actually wrong: recall, in two places

For *"people willing to relocate to vapi"* the true answer is **98 calls**.

| stage | count | lost |
|---|---|---|
| true matches (judge over all 609 calls) | 98 | — |
| candidates retrieval surfaced | 103 | **29 true matches never reached the judge** |
| judged a match | 69 | — |
| returned to the browser | **50** | **19 dropped by the client's own cap** |

Net: the user saw **50 of 98 (51%)**, and the Excel export built from that
selection (`call_exports` #4 and #5) contains exactly 50 rows.

**Loss 1 — `ARM_LIMIT = 40` in `search.py`.** Each retrieval arm could
contribute at most 40 calls to the fusion. The arms largely agree with each
other, so the union of six arms capped at 40 was barely wider than one arm.
Widening the candidate pool alone did nothing; this was the binding constraint:

```
ARM_LIMIT   pool   candidates   true found   recall
       40    120          103           68      70%
       40    400          103           68      70%   <- pool was never the limit
       80    250          128           85      87%
      150    250          195           98     100%
      300    400          400           98     100%
```

All 29 missing calls were embedded and retrievable — they simply ranked 41st or
worse in every arm. They skewed hard to the newest batch (2026-08-27), which is
what made it look like the 2026-08-25 truncation-by-id bug returning; it was
not, it was pure arm width.

**Loss 2 — the browser asked for 50 and rendered them as one page.**
`Intelligence.tsx` called `searchIntel(q, 50, agent)` and then set
`pages = nlRows ? 1 : …`, so an AI filter had NO pagination. 50 was therefore a
hard ceiling on what could be seen, ticked, exported or reported — regardless of
how many the server matched. The verification banner said "69 matched" directly
above a 50-row list, and nothing reconciled the two.

### 32.4 What was NOT wrong

**"Not willing to relocate" = 3 is correct.** Of the 115 calls where the agent
raised relocation, 97 agreed, 3 refused, and 15 answered ambiguously ("okay",
"maybe i will", "m.o.k.", or STT mush) and were correctly abstained on by the
judge. There genuinely were only three refusals — the agent's framing ("this is
an on-site role, if you are not residing in Vapi you would have to relocate")
draws agreement or a mumble far more often than a clean no.

**"Willing to relocate AND knows DevOps" = 6 is correct, and is a DATA finding,
not a search finding.** Splitting every transcript by speaker:

```
term        agent says it   CALLER says it
devops                 60                6
vapi                  115                6
relocate              115               39
```

The agent says "DevOps" 60 times because it reads a menu — *"Backend, Frontend,
AI, or DevOps Engineer"*. **The campaign never asks whether the candidate KNOWS
DevOps.** Only 6 callers ever said the word themselves, and the filter found
exactly those 6. No amount of retrieval or prompt work can recover a skill
assessment the call never performed; that is an agent-script change.

### 32.5 Precision was never the problem

Across six filters measured against full-corpus ground truth, **false
positives: 0**. Every row the filter showed genuinely satisfied it. The three
gates in `verify.py` are doing their job — the failure was entirely one of
hidden rows, which is the more dangerous direction: an empty seat looks like a
confident answer.

### 32.6 The export shipped unverified whenever the agent was named in the query

`intel_search_calls` lifts *"…using agent shubhangi"* out of the phrase and
judges on the remainder, storing verdicts under that LIFTED phrase. The client
sent the phrase the user TYPED. `load_evidence` matches on `lower(query)`, so
the lookup returned nothing, the Evidence sheet shipped empty and the workbook's
Summary said `verified: false` — silently downgrading a filtered export.
Measured: 50 evidence rows with the lifted phrase, 0 with the typed one.

Fixed in `export_create` (server-side, so every client is covered) with a
fallback through `parse_query_intent`, and in the client, which now sends
`nlApplied.query`.

### 32.7 A latent truncation-by-id, closed

`_lexical_arm` and its trigram fallback still had the exact defect fixed in
`_semantic_arm` on 2026-08-25: `DISTINCT ON (e.call_id)` forces `ORDER BY
e.call_id` to lead, so the `LIMIT 400` on that query truncated by ID, not by
rank. It had NOT fired on this corpus (the widest term matched 338 chunks
against the cap of 400), so it caused none of the observed loss — it was closed
because it is the same class and would bite as any account grows.

### 32.7b The ceiling had a SECOND floor: `hnsw.ef_search = 40`

Found only because the new arm-width test failed intermittently in the full
suite and the flake was chased instead of retried.

HNSW is an **approximate** index and stops early. `hnsw.ef_search` defaults to
**40** — the same number `ARM_LIMIT` used to impose, one layer further down
where raising `ARM_LIMIT` does nothing about it. On the test corpus (450
chunks, `LIMIT 4000`):

```
Index Scan using idx_call_embeddings_hnsw on call_embeddings e
  Order By: (embedding <=> $1)
  Filter:   (owner = 'search@test.local')      <-- post-index, NOT an index cond
→ 40 rows
```

Measured against the LIVE index, unfiltered so the plan is unambiguous:

```
hnsw.ef_search=40,  iterative_scan=off             LIMIT 1000 →   76 rows
hnsw.ef_search=400, iterative_scan=relaxed_order   LIMIT 1000 → 1000 rows
```

**76 of 1000 requested rows, silently.** Worse than a plain cap, because
`owner` is applied AFTER the index scan: HNSW walks its ~ef_search nearest
chunks across ALL tenants and only then drops other owners' rows, so a tenant
outside the global neighbourhood can get nothing at all.

This is **latent, not live**: at today's corpus size Postgres costs a
sequential/bitmap scan cheaper and picks it, and those are exact — which is why
the §32.9 recall numbers are real and why the observed 50-of-98 had nothing to
do with this. It flips to the HNSW plan as the table grows, with no error
anywhere. Fixed pre-emptively in `_tune_vector_scan`, `SET LOCAL` so it lasts
one transaction and cannot leak on a pooled connection, wrapped so an older
pgvector degrades instead of breaking search.

Which plan Postgres picks is cost-based and **moved between runs of the same
test file**, even with `enable_seqscan`/`enable_bitmapscan` off. So
`test_without_the_tuning_the_index_stops_at_ef_search` EXPLAINs first and skips
when the planner went elsewhere: a test that fights the planner flips, and a
flipping test is worse than none.

### 32.8b Several agents at once

The Agent control was a native `<select>`, so it held exactly one slug: *"how
did shubhangi AND maya do"* meant loading the page twice and adding the numbers
up by hand. It is now a checkbox popover (`kit.MultiSelect`).

`?agent=` carries a **comma-separated set on the SAME parameter** — a single
slug is a one-item list, so every existing link, bookmark and API caller keeps
working unchanged. `_agent_list()` is the one parser; `_call_filters`, the
analytics endpoint and `/intel/search` all moved from `agent_slug = ?` to
`agent_slug = ANY(?)`, so a one-agent filter and a twelve-agent filter take the
identical code path and cannot drift apart.

Because filter-built reports and exports go through `_call_filters`, they
inherit multi-agent selection for free. Verified on live data: shubhangi-outbound
188 + maya_lescall_sdr-outbound 133 = 321 for the pair.

The ConvAnalytics page has its own separate agent filter and was deliberately
NOT changed here.

### 32.8c Numbered pagination

`kit.Pagination` was Prev/Next only — fine for browsing, useless for a list you
are working through: reaching page 9 of 12 took eight clicks and the size of the
result was invisible. It now renders numbers with an elided window (first and
last always reachable, current always inside the window, gaps as an inert "…",
never more than 9 controls, so a 40-page list cannot wrap the toolbar).

It is the shared composite, so Reports, Contacts, Outbound, Recordings,
Follow-ups, Activity and Intelligence all gained it from one change.

### 32.8d The rate-limit flake that kept masking this

Two full-suite runs failed on `/v1` rate-limit tests while passing in isolation
every time. Not a product bug and nothing to do with §32 — but a suite that
cries wolf stops being usable as evidence, which is exactly how the ef_search
ceiling in §32.7b stayed hidden behind "probably just flaky".

Cause: `ratelimit.window_starts` buckets by WALL-CLOCK minute
(`now.replace(second=0)`), not a sliding window. A test that spends a 1-or-3
per-minute budget and then asserts a 429 gets a 200 whenever its requests
straddle a minute boundary. The `whole_minute` fixture waits out the tail of
the minute first. It does NOT freeze the clock: `_remember_block` caches a
refusal for 30 REAL seconds keyed by subject, so a frozen clock would keep one
test's block alive into the next test.

The limiter's wall-clock bucketing is left as-is — it is a deliberate design
(cheap, restart-safe in Postgres) and changing it was out of scope.

### 32.8 The changes

```
shared/intelligence/search.py    ARM_LIMIT   40 -> 150   (INTEL_SEARCH_ARM_LIMIT)
                                 CHUNK_POOL 1200 -> 4000 (INTEL_SEARCH_CHUNK_POOL)
                                 _tune_vector_scan(): hnsw.ef_search 40 -> 400
                                 (INTEL_SEARCH_HNSW_EF) + iterative_scan
                                 relaxed_order (INTEL_SEARCH_HNSW_ITERATIVE)
                                 lexical + trigram arms: LIMIT moved onto a
                                 rank-ordered subquery
shared/intelligence/config.py    FILTER_MAX_CANDIDATES 120 -> 300
                                 FILTER_CONCURRENCY     12 -> 16
                                 FILTER_BUDGET_S        60 -> 120
dashboard/intel_api.py           /intel/search limit cap le=100 -> le=500
                                 meta.returned + meta.pool_full (honesty signal)
                                 export_create: evidence fallback to the lifted
                                 phrase
frontend/src/api/intel.ts        Verification.returned / .pool_full
frontend/.../Intelligence.tsx    request 300 matches, not 50
                                 AI results PAGE client-side (were one page)
                                 header checkbox is page-scoped and additive
                                 "Select all N matches" across pages
                                 export sends the lifted phrase
                                 pool_full warning
                                 Agent filter is a MultiSelect (see 32.8b)
frontend/src/components/kit.tsx  Pagination: numbered, not Prev/Next (32.8c)
                                 MultiSelect: new shared composite
tests/conftest.py                `whole_minute` fixture (see 32.8d)
```

`pool_full` is the part that keeps this honest: when retrieval fills its own
candidate pool the UI now says `matched` is a FLOOR, not a total. A ceiling that
can be hit silently is the actual bug — 300 is only a bigger number.

### 32.9 After

Same six filters, same full-corpus ground truth, after the change:

```
query                                                  TRUE  cand shown recall  FP
people willing to relocate to vapi                       98   195    98   100%   0
candidates willing to relocate who can join in 30 days   85   186    85   100%   0
candidates who asked to be called back later             48   287    47    97%   0
candidates interested in the AI engineer role            60   182    60   100%   0
people who are not willing to relocate to vapi            3   257     3   100%   0
candidates willing to relocate to vapi and know devops    6   168     6   100%   0
```

Cold (no cached verdict, 194 candidates judged fresh): plan 2.4s + retrieval
1.6s + judge 13.9s = **17.8s**, not truncated, against a 120s budget. Warm, the
verdict cache serves the same filter in ~2s.

### 32.10 Deploy — DONE 2026-08-29

```bash
cd frontend && npm run build
docker compose -f deploy/docker-compose.services.yml up -d --build spa
docker restart lescall-api        # dashboard/*.py + shared/intelligence/*.py
```

No migration. No re-index: nothing about how embeddings are built changed, only
how many of them are read.

Verified live in the running container: `ARM_LIMIT=150`, `CHUNK_POOL=4000`,
`hnsw.ef_search=400 / relaxed_order`, `FILTER_MAX_CANDIDATES=300`,
`FILTER_BUDGET_S=120`, `le=500`, `pool_full` present, `_agent_list` parsing.
Suite 1620 passed / 1 skipped / 0 failed.

---

## 33. An outbound call greeted the wrong person (2026-08-29) — ROOT-CAUSED + FIXED

### 33.1 The report

> "Call `90d21bc9-6972-43d0-9be2-576469096799` was meant to be for the contact
> Amrit Raj but it fetched the previous contact name Harsh Agrawal, which lead
> to an issue during the call."

Not a data-entry problem and not a one-off. It is a **concurrency race**, and it
had been firing for six weeks.

### 33.2 The first thing the row says

```
id 698  conn_id 90d21bc9-…  call_key 916202469148_20260827_130704
        phone 916268617072  contact_id 961 (Harsh Agrawal)
```

`call_key` comes from the FreeSWITCH recording folder — the number actually
dialled — and it says **6202469148 (Amrit Raj)**. `phone` and `contact_id` say
Harsh Agrawal. The row disagrees with itself, which is the fingerprint.

### 33.3 Root cause

An outbound leg carries **no first-hand statement of who it reached**:

* the channel's caller id is OUR origination number
  (`origination_caller_id_number`), not the customer's;
* `Caller-Destination-Number` is the DIALPLAN EXTENSION
  (`OUTBOUND_DP_EXT = "outbound_ai_connect"`), because the destination goes in
  the dial string `sofia/gateway/<gw>/<phone>`. Sanitised to digits it is empty
  — hence `did=-` on every outbound line in the runtime log.

So `_lookup_recent_outbound_dial(caller_number)` compared dial records against
our own DID. **The exact-match branch could never fire on an outbound call**,
and the loop fell through to its last line:

```python
        if best_age is None or age < best_age:
            best, best_age = entry, age
    return best          # <-- the most recent dial in the last 120 seconds
```

During a campaign burst the most recent dial is *somebody else*. That record
then supplied, unchecked:

* `contact_name` → the agent opened with a stranger's name,
* `contact_phone` / `calls.phone` → and therefore `calls.contact_id`,
* `placed_by`, callback context,
* and `_fetch_customer_context(owner, dial["phone"])` — **one customer's call
  history handed to another**.

The guard already existed one layer up and was never applied here:

```python
    # - The loose most-recent dial match is fine for LABELING a call
    #   outbound, but may only steer AGENT selection on an exact phone match
    dial_phone_match = _dial_matches_phone(caller_number, dial)
    routing_dial_meta = dial if dial_phone_match else None
```

Agent selection was protected. **Identity was not.**

### 33.4 Blast radius — 44 calls, two tenants

Detector: `call_key`'s number ≠ `calls.phone`, compared on the last ten digits
(comparing the raw strings over-reports — a trunk prefix like `9122` in front
of the dial string is not contamination).

```
angelo@dhronai.com     40   2026-07-13 → 2026-08-27
support@milledger.com   4   2026-08-01 → 2026-08-03
```

24 of them landed on 2026-08-27, the day of the big campaign — the bug scales
with dial concurrency, which is exactly when it is least likely to be noticed.
Examples:

```
actually dialled → should have been      but was greeted as
6202469148         Amrit Raj             Harsh Agrawal        (the reported call)
9307603545         Saylee Bharsakle      SIDDARAPU ADARSH
9390681969         Eka Vardhan           Mohammad Ahmar
9000633061         NEERAJ … DUDABOYINA   ANIKAIT KAPOOR
```

Full list: `misattributed_calls.csv` (45 lines incl. header) in the audit
scratchpad.

Reproduced deterministically by replaying the old matcher over the real burst:
dialled Amrit Raj, matched **Harsh Agrawal**.

### 33.5 The fix — two halves

**Half 1: carry the destination on the channel.** `_fs_originate_cmd` now adds
`lescall_dest=<digits>`, forwarded by `socket_server.extract_rtp_info` and
ridden through as WS path segment 6
(`/<uuid>/<caller>/<did>/<campaign>/<slug>/<dest>`) to
`TelephonySession(dialed_number=…)`. A channel variable **cannot race** — it
belongs to that leg and no other. All five dial paths go through
`_fs_originate_cmd`, so all five are covered.

**Half 2: a dial record may only speak for the person on the call when its
phone matches.** `_lookup_recent_outbound_dial` now returns `_exact`, and
`_resolve_call_routing` splits the result in two:

* `dial` — the loose, time-only hint. May still LABEL direction.
* `identity_dial` — exact match only. The **only** one allowed to supply a
  name, a number, a contact, callback context, or a memory lookup.

`_customer_number()` is the single definition of "the other party": the caller
id inbound, the dialled number outbound, preferring a verified record's
normalized form (the dial string can carry a trunk prefix). `calls.phone`,
`calls.contact_id` and metering all key off it.

Half 2 is what makes this safe **even if half 1 is ever absent**: with no
verified match the agent simply asks who it is speaking to. That is a small
degradation; greeting someone by a stranger's name is not.

### 33.6 Deploy order matters

`dashboard/server.py` lives in `lescall-api`; the receiver and bridge live in
`lescall-runtime`. Deploy **API first**:

```bash
docker restart lescall-api        # starts sending lescall_dest
docker restart lescall-runtime    # starts reading it
```

API-first is harmless (an old receiver ignores the extra segment). Runtime-first
is also safe but degrades: no `lescall_dest` means no verified match, so
outbound calls run without a name until the API restarts. Neither order can
produce a wrong name.

### 33.7 Tests

`tests/test_outbound_identity.py` (16) — the dialled number selects its own
record; a time-only match is flagged and never becomes identity; the phone
guard across inbound/outbound/prefixed/empty; `_customer_number` per leg; and
`_resolve_call_routing` withholding `identity_dial` on the exact Amrit/Harsh
burst while still labelling the call outbound.

### 33.8 Not done

The 44 historical rows still carry the wrong `phone`/`contact_id`, so their
analytics and contact timelines are attributed to the wrong person. `call_key`
holds the truth, so a repair is mechanical — but it rewrites call history and
is the owner's call, not a silent migration.

## 34. Two calls to a real contact played silence — 6 funded Sarvam keys wrongly buried (2026-09-08) — ROOT-CAUSED + FIXED

### 34.1 Symptom

The two most recent outbound calls to a real contact (Angelo P Chery,
`+918076562864`, calls 785/786, 2026-09-08 07:25/07:28) connected, rang the
handset, and produced 20-26 seconds of complete silence before the agent hung
up. Both were logged as `answer_state=empty reason=no_caller_speech` — read as
"the callee never spoke" — and the phone-book row flipped to `empty_call`,
which reads as an unreachable number. The owner had confirmed independently
that plenty of Sarvam keys still had credit left, which did not match
`dashboard/sarvam_key_status.json` reporting `all_exhausted: true` with
`pool_remaining: 0`.

### 34.2 Root cause — verified, not inferred

Every one of the 28 keys in `sarvam_key_master.json` was on the dead list. A
direct probe of all 28 (one 2-character TTS call each) showed **6 were still
funded** (HTTP 200) and 22 were genuinely spent (HTTP 402). Two independent
defects, both in code the platform already had, combined to bury live keys:

**(a) `key_rotator.rotate()` popped one key per CALL, not per DEAD key.** One
exhausted key produces a *burst* of 402s — the STT router alone fires up to
four requests per audio segment (primary, its retry, the 0.6s race, its
retry), and TTS runs concurrently. Every 402 called `rotate()`, and every
call popped a fresh key off the pool regardless of whether the pool had
already moved on. `sarvam_key_watchdog.log` shows three keys consumed in six
seconds on 2026-08-31 (`13:03:23` → `13:03:44`), none of which had had a
chance to be *used*, let alone spent.

**(b) The watchdog buried a key on INFERENCE, never on evidence.** Its
in-process-rotation handler read as: "the active key in `.env` changed, so
whatever it changed *from* must be exhausted" — true only if defect (a)
hadn't already invalidated the premise. Every key that (a) discarded
unspent, (b) then declared officially dead, permanently, in
`sarvam_dead_keys.json`. Burial was also a one-way door: nothing ever
re-checked a dead key, so a refill (or a wrong burial) could never
self-heal.

**A third gap hid the whole thing from monitoring.** `shared/voice_health.py`
probes Sarvam every cycle but only checked for 5xx / timeout / 401/403 — HTTP
402 fell through and scored `"ok"`. The one system built to catch exactly
this reported the voice stack **healthy** for the entire 2026-08-31 →
2026-09-08 window while it was mute.

**A fourth gap let the platform keep dialling anyway.** Nothing at any of the
six outbound-origination call sites asked "can the agent even speak?" before
placing a real call — only billing/entitlement checks. A `no-credits` key
still let single dials, campaigns, autodial and automation callbacks ring a
handset and deliver silence, at the customer's expense of goodwill and the
tenant's expense of minutes.

**A fifth gap mislabeled the customer.** Call teardown's empty-call
classifier (`_compute_answer_state`) has no way to tell "the callee said
nothing" apart from "we never spoke to them" — both show zero caller
transcript. It always chose `no_caller_speech`, which mirrors onto the
contact as `empty_call` — an outage recorded as the customer's fault, on
their permanent record.

### 34.3 Fix

* **`key_rotator.rotate(failed_key=...)`** — the caller now passes the key
  that actually 402'd. `rotate()` only pops from the pool when that key is
  still the process's active key; if a peer already rotated past it, the
  new active key is returned instead of burning another. One dead key now
  costs the pool exactly one key, no matter how many requests are in flight.
  All three call sites (`stt/sarvam.py`, `tts_sarvam.py`, `tts_sarvam_ws.py`)
  updated.
* **`sarvam_key_watchdog.py`: verify before bury, and resurrect.** `probe_key()`
  fires one real TTS request and returns `live` / `dead` / `unknown` — `unknown`
  (5xx, network error, no httpx) can never bury or revive a key, so a Sarvam-side
  outage cannot empty the pool. Both burial paths (in-process rotation
  detected, 402 seen in the runtime log) now call `bury()`, which probes
  first. A background resurrection sweep (`resurrect()`) re-probes the dead
  list — every 30 min normally, every 5 min while `all_exhausted` — so a
  refill (or a wrong burial) heals without an operator. `SARVAM_VERIFY_BEFORE_BURY`
  is the kill switch back to the old inference-only behaviour.
* **`shared/voice_health.py`** — `_timed_post()` now returns `"no-credits"` for
  HTTP 402, `probe_bad` treats it as a failure, and a dedicated
  `voice-probe-credits` alert fires on the first probe (not after a rolling
  window) since every call placed while this holds is guaranteed dead air.
* **A voice-capability gate at every dial origin.** `dashboard/server.py`:
  `_voice_can_speak()` reads the watchdog's status file (fails OPEN on
  missing/stale/unreadable — never blocks a dial on its own account) and
  refuses when `all_exhausted` is fresh. Wired into all four *human/automatic*
  origination paths — single dial, test call, campaign loop (halts
  mid-run rather than dialling into a wall), autodial sweep, and the
  follow-up automation callback. The sentinel loopback probe stays exempt
  (synthetic, plays a WAV, rings no human).
* **Mute turns are now COUNTED, not inferred.** `SarvamTTS` tracks
  `spoke_utterances` / `mute_utterances` / `credit_blocked` per call — a
  `speak()` that emits zero audio chunks, or that has no key at all, is a
  mute turn. `TelephonySession._agent_was_mute()` reads that at teardown: if
  every utterance was mute and the caller never spoke, the reason is
  `agent_tts_unavailable`, not `no_caller_speech`, and the contact is **not**
  mirrored to `empty_call` for a fault that was never theirs. A single
  audible turn still clears the agent — a mid-call hiccup must never
  re-label a genuinely silent callee.

### 34.4 Recovery performed this incident

Probed all 28 keys directly; resurrected the 6 verified live
(`sk_yx74kd0i…`, `sk_nhoo1z14…`, `sk_m7zbrqq7…`, `sk_n5r2a8k0…`,
`sk_di4e8v9d…`, `sk_p94nhy6e…`) out of `sarvam_dead_keys.json`; promoted one
to active in `calling-agent/.env`. Verified end-to-end against the live key
through the production `SarvamTTS.speak()` path — real audio, `spoke=1
mute=0`. `lescall-api` and `lescall-runtime` restarted; `sarvam-key-watchdog`
and `lescall-sentinel` restarted onto the patched code. Calls 785/786's
`answer_state_reason` corrected to `agent_tts_unavailable` and contact 526's
`call_status` reverted from `empty_call` to `answered` — the only two rows
this defect touched.

### 34.5 Not done

The 22 genuinely dead keys need real refills or replacement in
`sarvam_key_master.json` — the resurrection sweep will pick them up
automatically the moment they're funded again, no restart required.

### 34.6 Tests

`calling-agent/backend/tests/test_sarvam_key_health.py` (17) — a burst of
402s on one key costs the pool one key; a 402 on a leased backup key never
touches the pool; verified burial/no-op-on-live/no-op-on-unknown; targeted
resurrection; the probe's 402→dead / 200→live / other→unknown mapping; the
sentinel probe scoring 402 as `no-credits`; and call-teardown classification
(mute agent → `agent_tts_unavailable`, real silent callee untouched, one
audible turn clears the agent).

## 35. Copilot scripted an agent perfectly — a Builder save silently un-scripted it (2026-09-08) — ROOT-CAUSED + FIXED

### 35.1 Symptom

Jai Mistry (outbound lead-qualifier for Craft Idea) was built through Copilot
with an exhaustively detailed, verbatim call script — every question, every
branch, every objection given an exact line to speak. The owner reviewed the
Copilot transcript: it was followed faithfully, step by step, and the
assistant reported "recorded verbatim" at every turn. The first real call
(09:51, 2026-09-08, to Angelo P Chery) did not resemble that script. The
caller said they weren't focusing on lead generation; the agent correctly
asked why; on the caller's (admittedly garbled) answer, instead of using any
of the four scripted acknowledgment lines and closing, it asked "anything
else I can help with, or should we end the call here?" — a sentence that
appears nowhere in the script.

### 35.2 Root cause #1 — the Agent Builder's Settings tab silently deletes stage wiring

`agent_revisions` held exactly one snapshot for `jai_mistry-outbound`,
timestamped **the same second** as the agent's `updated_at`, with `reason:
"builder full update"` and `actor: "…:builder"` — the audit trail of
`PUT /api/agents/{name}`, the classic dashboard's full-document-replace
endpoint, firing immediately after Copilot's own `create_agent` finished.

The snapshot's pre-write content (the file Copilot actually wrote) carried
correctly derived wiring on every stage — `role`, `collect`, `advance_when`,
`branches` — exactly what `_enrich_new_agent()`'s stage-wiring derivation
(`dashboard/agent_copilot_api.py`) is supposed to produce. The post-write
file had `role: ''`, `collect: []`, `advance_when: {}`, `branches: []` on
every stage. Something touched the Settings tab within that same second and
overwrote it.

The cause: `frontend/src/api/agents.ts`'s `SettingsTab` → `mergeWizardIntoDefinition()`
→ `compileStages()`. The Settings tab's "Call Script" section only ever
modeled `{id, goal, turns}` per stage — it has no fields for `role`,
`collect`, `advance_when`, `branches`, or `label`. `compileStages()`
rebuilt every stage from just those three fields, unconditionally, on
**every Settings-tab save** — not only saves that touched the Call Script
section. A save that changed nothing but, say, the company tagline still
called `out.call_flow = { stages: compileStages(d.stages) }`, replacing the
whole stage list. This is the same failure shape as the 2026-08-04 Kaapd
incident that `_enrich_new_agent()` was built to fix, reintroduced one layer
up: the backend derives good wiring, a completely different, older UI
component discards it on the very next save it touches, and nothing
connects the two to say so.

**Fix**: `compileStages(stages, existing)` now takes the stages already on
disk and merges the wizard's three fields onto a COPY of the matching
existing stage (by id) rather than rebuilding from scratch. A stage the user
adds in the Settings tab (no prior on-disk match) still gets a fresh,
unwired stage — there is nothing to merge it onto, same as before. Verified
with `tsc -b` (clean).

### 35.3 Root cause #2 — a stage goal telling the model to transition itself

Restoring the wiring alone was not enough. DISCOVERY's `advance_when` was
`{all_collect_filled: true (current_lead_gen + agency_status), max_turns: 6}`
— correct for the "yes, an agency handles it" path, wrong for "no, we're not
focusing on this at all": that path never asks about `agency_status`, so
`all_collect_filled` can never become true, and the stage could only escape
via `max_turns: 6`. Long before that, the stage's own goal text said, after
capturing the reason: *"Do not argue. Advance to closing."*

The model cannot "advance to closing" — a stage's goal is the only script it
is shown for a turn; the CLOSING stage's actual verbatim lines are never in
that prompt. Told to transition into an off-screen destination, the model
did what any instruction-follower does with an underspecified command: it
improvised one. That improvisation was the "should we end the call?" line —
plausible-sounding, entirely unscripted, and reachable from ANY stage whose
goal uses this phrasing whenever its own `advance_when` cannot fire for that
answer.

**Fix — a dedicated stage plus a synchronous branch, not a slower
`advance_when`.** DISCOVERY now branches to a new `NO_FOCUS_REASON` stage the
INSTANT the caller's answer classifies as `negation` (`detect_intent()` —
deterministic, regex-based, no LLM round-trip, already used platform-wide for
things like decline counting). Because `_maybe_branch()` runs before that
turn's response is generated, the caller's "no" is answered under
`NO_FOCUS_REASON`'s own goal (verbatim: ask why, say nothing else) in the
SAME turn — this is the existing STAGE-BEAT PLACEMENT behavior, just used
deliberately instead of accidentally. `NO_FOCUS_REASON` then converges on
`WRAP_UP` the turn the caller's reason is captured into a new `no_focus_reason`
slot, via a NEW branch primitive:

```yaml
branches:
- when_slot: {name: no_focus_reason}   # no `equals` — ANY answer
  goto: WRAP_UP
  max_takes: 1
```

`when_slot.equals` was previously required — exact-match only, useless for a
free-text reason with no fixed vocabulary ("budget issue" vs "leads already
coming" vs anything else a caller might say). `_maybe_branch()`
(`conversation.py`) now treats an omitted `equals` as "this slot has ANY
answer" — the caller has responded, converge, regardless of what they said.
`WRAP_UP`'s goal was rewritten to open with an acknowledgment rule (read the
caller's answer from CALL STATE, pick the matching one of four scripted
lines) BEFORE its existing closing script — so the turn that needs to close
the call always has the real closing text in front of it, not a `"advance to
closing"` instruction with nothing to advance to.

Verified end-to-end against the real LLM, replaying the actual failing
call's exact caller lines (`फोकस तो नहीं कर रहा`, then the garbled
`मन्नी है भाई`): the agent now asks the scripted "why" question, then closes
with the real verbatim script — no invented line, no wrong-stage detour.

### 35.4 Not done

The Q2 sub-paths ("no, we don't use an agency" / "it's in-house") still
carry the same free-text branch prose DISCOVERY always had, now reworded to
an explicit "say ONLY this line, then wait" (no more self-transition
language — the new lint below would catch it if it crept back in) but NOT
restructured into their own branched stage the way Q1's "no" path was. An
early exit here still reaches `AGENCY_CHALLENGES` (via the unchanged
`all_collect_filled` fallback) and would ask about a nonexistent agency's
"challenges" — a mismatched but *scripted, coherent* question, not an
invented one, and this exact path was never exercised on a real call.
Giving it the same `ASK_AGENCY` + branch treatment as Q1 is the natural
next step if it ever misfires live.

### 35.5 Platform hardening so this class doesn't recur

* **`_quality_warnings()`** (`dashboard/agent_copilot_api.py`) now flags any
  stage goal containing self-transition language ("advance to closing",
  "move to the next stage", "wrap up the call", …) that has no `branches` —
  i.e., a stage telling the model to transition itself with no real,
  immediate way to do so. Confirmed against the ORIGINAL (pre-corruption)
  Jai Mistry draft: fires on `DISCOVERY`/`AGENCY_CHALLENGES` exactly as
  written back then; silent on the fixed version.
- The Copilot system prompt's `branches` documentation now names the
  `when_slot` (no `equals`) primitive, and a new **EARLY-EXIT SCRIPTS**
  paragraph spells out the failure pattern directly — "ask X; if NO, ask why
  then close; if YES, ask Y then Z" is a branch-and-split shape, never one
  stage leaning on `all_collect_filled` across both exits — so a future
  interview with this exact shape gets built correctly the first time.
* `StageDefinition.branches`' docstring (`schema.py`) documents the
  `equals`-omitted meaning at the source of truth.

### 35.6 Tests

`calling-agent/backend/tests/test_jai_mistry_no_focus_convergence.py` (9) —
the exact caller utterance classifies as negation; the branch fires and
resets the turn counter; it does not fire on an affirmation or a second
time; `NO_FOCUS_REASON`'s prompt carries the real "why" line and never the
banned "advance to closing" phrase; convergence via the `no_focus_reason`
wildcard branch (not list-adjacency, which was verified — and caught by this
suite — to land on the wrong next stage); WRAP_UP's prompt carries both the
acknowledgment rule and the actual closing script; the real transcript's
unintelligible reason (`म्हटलं`) still converges. Frontend: `tsc -b` clean.

## 36. Even with wiring restored, the script wasn't followed — LLM script fidelity (2026-09-08) — ROOT-CAUSED + FIXED, PLATFORM-WIDE

### 36.1 Symptom

With §35's wiring fix live, two more calls to the same contact were placed.
The state machine transitioned correctly, but the SPOKEN content still
drifted from the owner's script: a caller's paraphrase of a scripted
objection ("tell me about your agency") got an entirely improvised pitch;
a verbatim closing acknowledgment ("Achha Sir, that's good to hear.") was
spoken correctly and then immediately padded with an invented "anything
else?" follow-up that looped the call; and after a caller gave an
unclassifiable reason for not using lead-gen, the agent skipped its own
scripted acknowledgment AND its closing script entirely, asking a brand new,
never-authored question ("what's your biggest challenge bringing leads?")
instead.

None of this was a wiring problem — the state machine put the model on the
right stage every time, with the right verbatim line sitting in that turn's
prompt. The model chose not to use it.

### 36.2 Root cause #1 — verbatim scripted answers were unreachable for the very callers they were written for

`response_bank.objection_playbook` entries ("what do you do", "are you an
AI", "how did you get my number") are matched by
`_match_objection_playbook()` — but that function is called ONLY from
inside `if intent == "objection"`. `detect_intent()` correctly classifies
"what do you do"/"tell me about your company" as `question`, not
`objection` — it is not a decline. The result: an FAQ-shaped playbook entry
could only ever fire if the caller's neutral question ALSO happened to read
as a decline, which is close to never. The exact scripted answer was
sitting in the live prompt's ━━ OBJECTION HANDLING ━━ section every turn,
structurally unreachable through the one deterministic path built to speak
it.

Compounding this: EVERY pattern in the objection playbook (and in the
platform's own `_DEFAULT_OBJECTION_PATTERNS`) was authored in romanized
Hinglish — because that's how the owner dictated it to Copilot, and how
Copilot wrote it down. Sarvam STT transcribes fluent Hindi speech in
**Devanagari script**. A caller who actually asked "अपनी एजेंसी के बारे में
बताओ" could never match a pattern list of `"what do you do"` / `"kya karte
ho aap"` by substring, in either direction — this holds for any hi/gu agent
with romanized-only patterns, not just Jai Mistry.

**Fix**:
* A new, intent-independent check — right before the existing objection
  branch, gated on `intent != "objection"` so genuine declines
  (NOT_INTERESTED, DND, decline-counting, callback policy) are completely
  unaffected — tries the playbook on every non-objection turn too. A
  paraphrase of a scripted FAQ now gets the exact verbatim answer,
  deterministically, with no LLM call at all.
* Jai Mistry's playbook patterns now carry Devanagari forms alongside the
  romanized ones, including word-order-flexible fragments (`"एजेंसी के बारे
  में"`, not only `"...बताओ"` glued to the end).
* `dashboard/agent_copilot_api.py`'s system prompt now tells Copilot
  explicitly: author every objection_playbook / objection_patterns /
  time_patterns entry in BOTH scripts whenever the agent speaks hi/gu, and
  that these entries fire independent of objection classification.
* A new `_quality_warnings()` lint flags any objection_playbook entry whose
  patterns contain zero Devanagari/Gujarati characters on an agent that
  supports hi/gu. Verified: fires on the original (pre-fix) Jai Mistry
  playbook (all 6 entries), silent on the fixed one.

### 36.3 Root cause #2 — nothing told the model a "verbatim" line was a literal contract

A stage goal saying `Say verbatim: 'X'` is, to the model, an adjective, not
an enforced contract — nothing in the system prompt ever explained what
"verbatim" means or that padding a correct line with an extra sentence is
itself a failure. The model treated every scripted line as a strong
suggestion to build on, which is exactly what a generally helpful assistant
is trained to do with a quoted example.

**Fix**: `script_fidelity_block()` (`prompt_blocks.py`) — a new, short,
always-rendered block placed immediately before ━━ CURRENT GOAL ━━ (closest
possible position to the caller's turn, for maximum recency) that states
plainly: a quoted "verbatim"/"say ONLY" line, or an OBJECTION HANDLING
trigger→answer pair, is spoken word-for-word, nothing prepended or
appended, and an OBJECTION HANDLING trigger matches on MEANING, not exact
wording. Platform-wide — every agent's prompt now carries this, not just
Jai Mistry's.

### 36.4 Root cause #3 — CALL MISSION competed with, and beat, the closing goal

`mission_block()` renders `━━ CALL MISSION ━━` with a blanket "every turn
must move the call toward this mission" — present on every single turn,
including the one where CURRENT GOAL is the closing script. On the call
that skipped its own acknowledgment and closing lines entirely, the model's
invented question ("what's your biggest challenge bringing leads?") is
exactly the kind of move a broad "identify marketing pain points" mission
would suggest — it just wasn't what that turn was for.

**Fix**: `mission_block()`'s standing instruction now explicitly carves out
CURRENT GOAL: when the goal is closing the call or hands the model a
specific line, follow that instead of finding one more qualifying question
to ask. Also platform-wide.

### 36.5 Verified

Replayed the actual failing exchanges against the real LLM (not the stub):
the "tell me about your agency" paraphrase now returns the exact scripted
EXACTLY_WHAT_YOU_DO line, byte-for-byte, via the new deterministic shortcut
(confirmed directly against `_match_objection_playbook()`, bypassing the
model entirely); the identity-confirm beat now reproduces "Jai Mistry here
from Craft Idea..." instead of an invented alternate greeting; the
"everything is fine" acknowledgment renders without an appended follow-up
question in the same turn.

### 36.6 Not done / residual

Convergence from `NO_FOCUS_REASON` to `WRAP_UP` (§35's fix) depends on an
async LLM slot-extractor completing within the turn; it did in most replay
runs but not every one — when it lags, the `max_turns: 1` escape lands on
`AGENCY_CHALLENGES` instead, which is now a real scripted question ("Current
agency ke saath kaunsi challenges…") rather than an invented one, but still
not the intended immediate close. Making that convergence deterministic
regardless of extractor timing (e.g. a synchronous capture for the specific
"reason" slot) is the natural next step if it's seen live.

### 36.7 Tests

`calling-agent/backend/tests/test_faq_playbook_shortcut.py` (8) — a
paraphrased FAQ question gets the verbatim playbook answer; the exact
authored trigger still works; a genuine objection still increments
decline_count and never gets a FAQ answer; an objection phrase that also
happens to match a playbook pattern still counts as a decline (no
double-counting regression); SCRIPT FIDELITY renders in the live prompt,
positioned before CURRENT GOAL; CALL MISSION's closing carve-out is present.
Full backend suite green.

## 37. The next call still went wrong — six defects, none of them stage wiring (2026-09-08) — ROOT-CAUSED + FIXED, PLATFORM-WIDE

§35 fixed the wiring. §36 made the model use the script it was shown. The
call placed at 20:57 that evening (`calls.id` 791, `conn_id`
`7db6c762-fea0-4e2a-97a3-522bc7874f9d`, jai_mistry-outbound rev 16, 140.6s)
still repeated itself three times, answered a Hindi caller in English,
ignored an answer he gave twice, spoke its own punctuation aloud, and would
not hang up when he said "कर दो" four times.

None of it was stage wiring, and none of it was specific to this agent. Six
separate platform defects, all present on every agent, all fixed here.

### 37.1 Symptom — the transcript

```
[00:00] Agent : Hello, kya main Angelo P Chery Sir se baat kar raha hoon?
[00:06] Caller: हेलो, हां जी बताओ।
[00:08] Agent : Thank you Sir. Main bas ek quick sa check karna chahta tha — kya aap …focus kar rahe hain?
[00:19] Caller: अभी तो ज़्यादा नहीं कर रहा हूँ, क्यों?
[00:21] Caller: बताओ आप कहाँ से बात कर रहे हो?
[00:22] Agent : Right, actually मैं Craft Idea से बात कर रहा हूँ। क्या कोई particular वजह रही …focus ना करने की?   ← ask #1
[00:32] Caller: हा.
[00:36] Caller: नहीं, हम एक्चुअली ज़्यादा उस पर एम्फसाइज़ करते हैं नहीं।     ← NEVER REACHED THE ENGINE
[00:37] Agent : अच्छा Sir, तो क्या वजह रही है अब तक इस पर …focus ना करने की?   ← ask #2
[00:42] Caller: आप कौन सी कंपनी से बात कर रहे हो?
[00:42] Agent : This is Jay Mistry from Craft Idea. How can I help you?        ← English, inbound-shaped, 2ms (deterministic)
[00:55] Agent : समझ गया Sir. Koi particular वजह रही अब तक इसपे focus ना करने की?  ← ask #3
…
[01:49] Agent : 'Aapke time ke liye thank you. Have a great day.' ठीक है, तो फिर मैं कॉल कट कर दूँ?
[02:02] Agent : माफ़ कीजिए, मुझे आपकी आवाज़ नहीं आई। 'Aapke time ke liye thank you. …' ठीक है, तो फिर मैं कॉल कट कर दूँ?
[02:11] Caller: कर दो, कर दो, कर दो, कर दो।
[02:15] Agent : 'Aapke time ke liye thank you. Have a great day.' ठीक है, तो फिर मैं कॉल कट कर दूँ?
```

STT was flawless throughout — every caller utterance is in the log,
transcribed correctly. Nothing here is a hearing problem.

### 37.2 Root cause #1 — the agent's own first stage had NEVER run, on any call

`_post_greeting_stage()` (conversation.py) read:

```python
if not stages or any(s.id == STAGE_DISCOVERY for s in stages):
    return STAGE_DISCOVERY
return stages[0].id
```

It asked whether a stage named DISCOVERY existed **anywhere**, not whether it
was first. jai_mistry's flow is `INTRO_QUALIFY(0) → DISCOVERY(1) → …`, so
every call jumped straight to stage 1. `INTRO_QUALIFY` — role
`confirm_person`, carrying the verbatim `'Jai Mistry here from Craft Idea.
Sir, kya ek minute baat kar sakte hain?'` and the entire wrong-number script
the owner spent two Copilot turns dictating — had never governed a single
turn.

That is why the agent never said who was calling, and why the caller asked
twice ([00:21] and [00:42]). Two of the three "repeated" turns trace back
here.

**Also affected:** `jethalal-{inbound,outbound}` (CONFIRM_PERSON) and
`lescall_com` (PATTERN_INTERRUPT) — same shape, same dead opening stage.

Fixed: the flow's own first stage (by `order`) wins. Deliberately scoped —
a flow whose first stage is literally named `GREETING` keeps today's
behaviour exactly, quirks included (DISCOVERY when the flow has one:
pramukh; its own stage 0 when it does not: fitcoach, kaapd_pooja,
serene_health, whose stage-0 goals are written for the beat after the caller
answers). Verified across all 45 agents: exactly 5 change, all of them the
broken shape.

### 37.3 Root cause #2 — "कहाँ" (where) contains "हाँ" (yes)

```python
if any(w in lower for w in ["हां", "हाँ", "जी", "ठीक", "હા", "બરાબર"]):
    return "affirmation"
```

A bare substring test. `\b` cannot help here — every Devanagari code point is
a `\w`, so there is no word boundary between "क" and "हाँ".

`"बताओ आप कहाँ से बात कर रहे हो?"` therefore classified as **affirmation**.
So did every Hindi where/here/there/wherever: कहाँ, यहाँ, वहाँ, जहाँ — and
जी inside जीवन, राजी. Platform-wide, every hi/gu agent, since the rule was
written.

On this call it meant DISCOVERY's `when_intent: negation` branch could not
fire on the caller's actual "no", leaving the model in DISCOVERY to
improvise — ask #1 and ask #2.

Fixed: `_AFFIRM_INDIC_RE`, a hand-rolled Indic word boundary
(`(?<![Indic])…(?![Indic])`), longest alternatives first so "हाँजी" still
matches.

### 37.4 Root cause #3 — the engine waited 4s for the caller's answer, then threw it away

```
20:58:15.983 [ENGINE] Merge window held 550ms (fragment 'हा.', caller quiet only 402ms — split likely)
20:58:16.535 [ENGINE] Merge window re-armed — caller input still in flight   ×7
20:58:19.953 [ENGINE] Dropping old segment (4.0s gap): 'नहीं, हम एक्चुअली ज़्यादा उस पर एम्फसाइज'
20:58:19.954 [INTENT] "हा." -> general
```

Two independent numbers that had to agree and did not:

* `MERGE_HOLD_MAX_TOTAL_S = 6.0` — how long the hold may re-arm *because the
  caller is demonstrably still speaking*.
* `_gap_cap = 3.0` — how old a follow-on may be before it is discarded.

Every follow-on arriving between 3s and 6s of holding — precisely the ones
the re-arm loop exists to collect — was dropped. The engine answered the
3-character fragment "हा." instead, and re-asked the question the caller had
just answered. This is the whole of "not taking in my input", and it is
platform-wide.

Fixed: `_gap_cap = max(_gap_cap, time_held + 0.5)` — the cap now covers the
whole window we were willing to wait. Short holds (the common case) are
unchanged at 3.0s. The log line said "Dropping old segment" while dropping
the NEW one; reworded, it cost real time during this investigation.

### 37.5 Root cause #4 — the close-confirmation gate could not read "do it", and asked twice

```
20:59:55.401 [HANGUP] close-confirm answered 'कर दो, कर दो, कर दो, कर दो।' — not a goodbye, conversation continues
```

`_is_close_confirm_yes()` looks for a word naming the action (end / cut /
खत्म / बंद / रख). But the question already named it — "shall I cut the
call?" — so the caller answered with a bare imperative, "do it", four times.

Worse, the gate then dropped and the turn re-entered the LLM, which
re-emitted `[END_CALL]` with the **identical** farewell, and
`_maybe_close_confirm` armed a second time (`_CLOSE_CONFIRM_MAX = 2`).
Nothing remembered the goodbye had already been spoken.

Fixed, both halves:
* imperative "go ahead / do it" forms in hi/gu/en are consent — safe because
  this test only ever runs on the turn right after we asked the question;
* `_close_confirm_answered` — once the caller has responded to a close
  question, a later end-call signal closes directly. One check guards
  against a mishearing; a second is a loop.

### 37.6 Root cause #5 — dead-air repair replayed the farewell

`generate_silence_reprompt()` prefixes "I couldn't hear you" and replays
`last_agent_response`, condensing only if it is over 90 chars. The pending
line was 83, so the whole thing came back — farewell included.

Fixed: with a close-confirm pending, re-ask **only** the confirm question,
and without the apology — the caller is being asked a yes/no question, not
asked to repeat themselves.

### 37.7 Root cause #6 — delivery: English accent on Hindi lines, spoken quote marks

Two separate delivery defects, both audible.

**(a) `_pattern_score` scored the agent's own Hindi as English.** It never
stripped punctuation (its sibling `stt_mirror_consistent` does), and
`_HINDI_PATTERNS` carries content words (kya, baat, kaun) but none of the
function words that make a Hinglish sentence Hinglish. Measured on this
agent's own configured lines, **4 of 10 scored zero** and went through
Sarvam's ENGLISH pronunciation frontend:

| line | old | new |
|---|---|---|
| `Aapke time ke liye thank you. Have a great day.` | **en** | hi |
| `Achha Sir, samajh gaya.` | **en** | hi |
| `Bilkul Sir, koi problem nahi. Aapke liye kaunsa time convenient rahega?` | **en** | hi |
| `Maaf kijiyega shayad galat number lag gaya hoga. dhanyawad` | **en** | hi |

Fixed: punctuation stripping, plus `_ROMAN_INDIC_TTS_MARKERS` — a **third**
list, deliberately not an extension of `_HINDI_PATTERNS`/`_GUJARATI_PATTERNS`
because those also feed the caller-side evidence language switcher, where
widening them would change which language a CALLER is heard in. This one is
read by `tts_language_for()` alone. Every entry is a token that is not an
English word (`is`, `us`, `me`, `the`, `hi`, `sir`, `problem` all excluded on
purpose); genuine English lines on a Hindi call still speak English.

**(b) The model spoke the quotation marks.** §36's SCRIPT FIDELITY block
tells it to reproduce a quoted line word for word; it duly reproduced the
delimiters too. Fixed at both ends: the block now says the marks are
delimiters, and `_strip_script_quotes()` in `_validate_response` removes them.
Boundary-aware, because responses stream clause by clause and the marks
routinely arrive unpaired — an apostrophe inside `that's` and a real mid-
sentence quote both survive.

### 37.8 Root cause #7 — the identity answer was English and inbound-shaped

`intent == "identity_question"` fell back to a hardcoded
`f"This is {agent} from {company}. How can I help you?"`. On a Hindi
**outbound** call that is wrong three ways: English mid-Hindi-call, an
inbound offer of help from the side that placed the call, and no return to
the question the agent had just asked — so the script stalled and the model
improvised the next turn.

Fixed: `_identity_answer()` builds from the same localized `_INTRO_LINES`
table as the connect intro, direction-aware; on outbound it re-anchors on
`_last_question_asked()` instead of offering help. The agent also now carries
a `WHICH_COMPANY` playbook entry with the caller's actual wording in both
scripts.

### 37.8b Root cause #8 — a Hindi caller could not decline

Found while replaying the fixed call: the agent said goodbye twice on the way
out. The cause is not the closing script.

`_DEFAULT_OBJECTION_PATTERNS` is **100% Latin** — "not interested", "abhi
nahi", "baad mein baat". A Hindi or Gujarati caller declining in their own
language matched none of it, so `decline_count` never incremented,
`call_flow.decline_threshold` was never reached, and the decline farewell /
DND path never ran. Only three narrow Devanagari phrases were hardcoded
alongside it.

Measured before the fix, on the live agent's own effective pattern list:

| caller says | meaning | classified |
|---|---|---|
| `लेकिन मैं अभी इंटरेस्टेड हूं नहीं …` | not interested | `negation` |
| `मुझे इंटरेस्ट नहीं है` | not interested | `negation` |
| `अभी बात नहीं कर सकता` | can't talk now | `negation` |
| `बाद में बात करते हैं` | call me later | **`general`** |

So the agent kept pitching someone who had already declined — and on this
call the caller's "not interested" fell through to the FAQ playbook (whose
answer is itself a farewell) and then to WRAP_UP, which said goodbye again.
That is the last of the repetition.

Fixed platform-wide: native-script phrases added to
`_DEFAULT_OBJECTION_PATTERNS` (so `objection_patterns_mode: extend` hands
them to every agent, and an owner reading the list in the builder can see
both scripts are covered), plus `_DECLINE_INDIC_RE` — word-WINDOW regexes
rather than fixed phrases, because Hindi word order moves freely (the live
caller said "इंटरेस्टेड हूं नहीं", not the dictionary "इंटरेस्टेड नहीं हूं").

Every alternative requires an interest / need / time word beside the
negation, so a plain "no" ANSWER to a qualification question
("नहीं, हमारे पास एम्प्लॉइज नहीं है") stays a `negation` — the branch engine
depends on that. Verified in both directions, 15 phrases.

The Copilot lint that catches romanized-only `objection_playbook` patterns
(§36) now covers `knowledge_base.objection_patterns` too — but only when the
list REPLACES the defaults, since `extend` is now covered by the platform.

### 37.8c Root cause #9 — a convergent stage escaped into the wrong stage

Found on the verification replay, after §37.8b changed the decline to be
heard: the caller declined, the scripted decline answer was spoken, and the
next turn landed in **AGENCY_CHALLENGES** — a stage with no script for that
caller — where the model improvised an English *"Is there anything else I can
help you with?"*.

`NO_FOCUS_REASON` converges on `WRAP_UP` through the §35 `when_slot` branch,
which needs an async slot extractor to complete inside the turn. When a
deterministic shortcut answers the turn instead, the extractor never runs, and
the `max_turns` escape falls back to **list adjacency** — and the stage sitting
next in the YAML is AGENCY_CHALLENGES. This is the §36 residual ("when it lags
the max_turns escape lands one stage short") reproducing, and it cannot be
fixed by reordering: NO_FOCUS_REASON and AGENCY_CHALLENGES both converge on
WRAP_UP, and only one of them can be adjacent to it.

Fixed with a new primitive rather than another one-off: **`advance_when.goto`**
names where a stage's advancement LANDS. Absent = list adjacency, exactly as
before; a `goto` naming no real stage logs a warning and falls back rather than
stranding the call. Documented in `schema.py` so Copilot can use it.

Both convergent Jai Mistry stages now carry `goto: WRAP_UP`, and WRAP_UP's goal
gained one rule: if a goodbye was already spoken this call (a scripted decline
answer is one), skip the second farewell speech and go straight to the final
line. That was the last of the repetition.

### 37.8d Verified end-to-end against the real LLM

The actual caller turns of call 791, replayed through the deployed engine with
nothing stubbed:

| # | caller | agent |
|---|---|---|
| 1 | हेलो, हां जी बताओ। | identity confirm (INTRO_QUALIFY — previously never ran) |
| 2 | …बताओ आप कहाँ से बात कर रहे हो? | the owner's Hindi identity line |
| 3 | आप कौन सी कंपनी से बात कर रहे हो? | the same scripted Hindi line |
| 4 | नहीं, हम …एम्फसाइज़ करते हैं नहीं। | the verbatim DISCOVERY question |
| 5 | नहीं, हमारे पास एम्प्लॉइज नहीं है… | the verbatim "why" — **asked once**, not three times |
| 6 | …इंटरेस्टेड हूं नहीं… | the owner's scripted decline answer |
| 7 | ठीक है, ठीक है। | `Aapke time ke liye thank you. Have a great day.` + the close question |
| 8 | कर दो, कर दो, कर दो, कर दो। | **closes** — `hangup_pending=True` |

No repetition, no English on a Hindi call, no spoken quote marks,
`tts_lang=hi` on every turn, and it hangs up the first time it is asked to.

### 37.9 The Copilot side — why the agent needed repairing at all

`agent_revisions` 212→214 (all `angelo@dhronai.com:copilot`, 18:28–18:29)
show the same failure three times: the owner dictated new wording for part of
a free-text field, and Copilot **regenerated the whole field**, silently
dropping constraints nobody had asked it to remove.

| rev | field | silently deleted |
|---|---|---|
| 212 | `knowledge_base.portfolio_prompt_text` | "Never state or invent specific numbers for total leads, active clients, or ROI" + the deflection line |
| 213 | `AGENCY_CHALLENGES.goal` | "say ONLY the matching line below, then wait"; "Do not ask about ending the call" — §35/§36 hardening |
| 214 | `WRAP_UP.goal` | the §35 acknowledgment rule; added "Then terminate the call immediately" |

Rev 213 also reintroduced **"and advance to WRAP_UP"** on a stage with
`branches: []` — the exact phrasing §35's `_quality_warnings()` lint was
written to catch. The tool result came back `"quality": []`, because that
lint sits behind `include_wiring_lints`, which is `False` for `update_agent`
and `update_stage` — every path an agent is actually edited through.

A fourth failure, at message 112: the owner pasted a line the agent had
**spoken** and asked to shorten it. `find_text` returned 0 matches (the line
was composed by the model, not configured). Copilot did not say so — it
invented a `never_rules` entry about a phrase it had never located and
reported *"The trailing 'Thank you Sir.' has been completely removed."*

Fixed:
* the self-transition lint now runs on updates, scoped to the stages that
  write **touched** (`_changed_stage_ids`) — linting what a turn just wrote
  is never a nag — and now also catches "terminate the call";
* system-prompt rule **e5 EDIT A FIELD, DO NOT REGENERATE IT** — read the
  current text, keep every existing constraint verbatim, splice the new
  wording in, and ask before deleting a constraint;
* system-prompt rule **e6** — never write a transition or hangup instruction
  into a stage goal;
* system-prompt rule **A QUOTED LINE find_text CANNOT FIND WAS IMPROVISED,
  NOT CONFIGURED** — say so plainly, fix the cause, and never report a phrase
  as removed that was never there.

### 37.10 Agent repair (jai_mistry-{outbound,inbound} rev 17 / 9)

Restored the acknowledgment rule and the say-ONLY guards; removed
"terminate the call immediately" and "advance to WRAP_UP"; gave
AGENCY_CHALLENGES a real `when_slot` branch to WRAP_UP; restored the
no-invented-numbers rule into the knowledge paragraph alongside the owner's
new text; added the `WHICH_COMPANY` playbook entry and word-order-flexible
Devanagari patterns for NOT_INTERESTED ("इंटरेस्टेड हूं नहीं" — the caller's
actual word order, which the existing suffix-glued patterns could not match);
replaced the over-specific never_rule with a general one.

### 37.11 Not done / residual

* `"अभी तो ज़्यादा नहीं कर रहा हूँ, क्यों?"` (a "no" **and** a question)
  classifies as `question`, because the Question rule precedes Negation in
  `detect_intent`. That is defensible — the caller genuinely asked something
  — and §37.2 + §37.8 make it moot on this call (the agent now introduces
  itself, so the caller has no reason to ask). Reordering intent precedence
  platform-wide was judged too broad for this incident.
* `llm_config.model: gpt-4o-mini` in the YAML is dead — `LLM_MODEL` env wins
  (`gemini-3.5-flash` on this call). Harmless at runtime, misleading in the
  builder UI.
* The Q2 sub-paths ("no agency" / "in-house") still converge via
  `all_collect_filled`; still not branch-restructured (carried from §35).

### 37.12 Tests

`calling-agent/backend/tests/test_call791_turn_taking.py` (44) — first stage
governs the first turn, and the two GREETING-first shapes are provably
untouched; Indic affirmation is a word not a substring; the merge cap covers
the whole hold; imperative consent closes and the gate asks once; dead-air
after a close question re-asks only that question; romanized-Hindi script
lines route to the Hindi frontend while English ones do not; delimiter quotes
are stripped and real ones survive; the identity answer is Hindi, outbound-
shaped, and the caller's actual question hits the playbook.

`calling-agent/backend/tests/test_copilot_quality_lints.py` (+5) — the
self-transition lint fires on the rev-213 regression and on "terminate the
call", stays silent for a stage that really branches there and for untouched
stages, and the three new system-prompt rules are present.

`test_jai_mistry_no_focus_convergence.py::test_wrap_up_carries_the_real_closing_script_and_the_ack_rule`
was pinned to a literal the owner had legitimately changed through Copilot;
it now derives the expected lines from the live definition and tests the
wiring instead.

All 90 `pramukh` golden prompts regenerated (`GOLDEN_REGEN=1`) — reviewed
first: a single added SCRIPT FIDELITY line, uniform across all 36 cases,
nothing else moved.

---

## 38. Admin plane: number handover + reward minutes (2026-09-09) — SHIPPED, LIVE

Two operator capabilities that only existed as `psql` sessions, plus the live
handover of `+91 80 3133 9140` that prompted them.

### 38.1 The handover, and why moving one row is not a transfer

`info@craftidea.in` → `craftidearesearch@gmail.com`, same company, both accounts
already on the platform. The number's row was the obvious thing to change and
the *least* of what had to move. Three separate ownership checks sit on the live
path and all three are read at dial time:

| what | who reads it | what breaks if it stays behind |
|---|---|---|
| `phone_numbers.owner` | `_did_owner()`, `billing_owner()` | the wrong tenant is billed; the caller is matched against the wrong contact book |
| `trunks.owner` | `server._outbound_identity()` | the number answers calls and can **never place one** — "its trunk belongs to another account" |
| `agents.owner` | `_outbound_identity` → `_agent_owned_by()` | same: refused before the originate |

So a transfer moves the number, its trunk and the agents bound to it, or it is a
half-migration that looks finished in the console and fails on the first
outbound call.

**The trunk name is also the FreeSWITCH gateway name**, and it is shown to the
tenant in their own Telephony panel — leaving `info-craftidea-in_plivo-339140`
on the receiving account puts the previous owner's email address in their UI.
`telephony_api.update_trunk` refuses renames for a good reason (the XML would be
orphaned and every live DID would point at a gateway that no longer exists), so
the rename is done properly: write the new gateway XML, `reloadxml` +
`sofia profile external rescan`, verify the new gateway is `UP`, then
`killgw` the old one and delete its file. `reloadxml`/`rescan` **only ever add**
gateways — without `killgw` the retired one keeps accepting originates until
FreeSWITCH restarts.

What did NOT move, deliberately: `calls`, `usage_ledger` and `contacts` rows for
the old account. Those are the record of what that account did.

The DID's dialplan (`ai_inbound_8031339140.xml`) needed no change at all — it is
keyed on the number, not the owner, and routes to `127.0.0.1:9000` regardless.

Verified after the move:

```
inbound  agent: fitcoach_ai-2-inbound        # resolve_agent_slug(did=…)
outbound agent: fitcoach_ai-2-outbound
did owner     : craftidearesearch@gmail.com
outbound id   : gateway craftidearesearch-gmail-com_plivo-339140, caller 918031339140
dial gate     : ALLOWED (growth, 8000 min, telephony=1)
```

### 38.2 `/admin/telephony` — the operator's number plane

`shared/telephony_admin.py` is the logic; the HTML console
(`dashboard/admin_telephony.py`) and the JSON API (`/admin/api/...` in
`admin_app.py`) both call it, so the two surfaces cannot drift on who may bind
what. Per account: add a number, add a trunk, rebind inbound/outbound agents,
**Connect** (write gateway + dialplan, reload), **Disconnect** (delete the
dialplan, keep the row and its bindings), **Remove**, **Transfer**.

Two rules are enforced in the module, not in the page:

* **The receiving account must be entitled to telephony** — `telephony_allowed()`,
  the same predicate the tenant's own panel and the dial gate use, re-checked at
  Connect and not only at create. A number parked on a Free account answers calls
  the billing gate then refuses to meter.
* **A number may only bind to agents in its OWN account.** The inbound resolver
  *trusts* the binding and never re-checks ownership (`resolve_agent_slug`), so a
  cross-tenant binding is RUNBOOK §33 from the other end: another tenant's agent
  answering in their own company's name, billing their own minutes.

`freeswitch_config.remove_inbound_dialplan()` / `remove_gateway()` are new. Both
delete endpoints previously said *"generated config file (if applied) is not
auto-removed; see deploy/RUNBOOK.md"* — fine as a note a human reads, not fine
once a console button does it: a "removed" number kept answering calls.

### 38.3 Reward minutes — and the pool that had to be built

`/admin/tenant/<owner>` → **Reward minutes**: N minutes, valid M months, audited
reason. It runs on the existing `minute_topups` rail (`source='admin_credit'`,
`amount_paise=0`, no gateway order), so `get_entitlements()` folds it into
`minutes_limit` and every gate, meter and invoice picks it up with no other
change. No payment row and no invoice — no money moved, and a ₹0 receipt would
put a fake sale in the GST series.

**The defect that had to be designed out.** A paid top-up expires with the period
that sold it, so "add N to this period's ceiling" was always exact — there is
only ever one period. Validity in MONTHS breaks that: `minutes_limit` is
recomputed as `included + topup` at every rollover, so *1000 minutes for 3
months* would have handed out **3000**. The pool would silently refill.

So a grant drains. `minutes_consumed` + `settled_through` on the row;
`credits.settle_owner()` walks the CLOSED IST calendar months in the grant's
window and charges `max(0, minutes_used − minutes_included)` — only what the
tenant spent **beyond their own plan** — against the oldest grant covering that
month. The live period is never settled: its overspend is already bounded by
`included + remaining`, and settling it would double-count. The entitlement read
became `SUM(GREATEST(minutes - minutes_consumed, 0))`.

Wired into `sync.run_once()` as the `credits` pass, not a cron: if it never ran,
a multi-month grant would behave exactly like the bug.

Two honest limits, both documented in the module: the settlement grid is the IST
calendar month (historical period boundaries are recorded nowhere in this
schema), and it charges against the tenant's CURRENT `minutes_included`, so a
plan change mid-grant settles at the new size.

A grant younger than one calendar month has nothing to settle and behaves
exactly like today's paid top-up — the common case stays on the old fast path.

**Testing note.** The obvious test (*grant 1000/3mo, burn it, roll over, assert
the topup is 0*) passes even with the bug, because settlement marks a fully
drained grant `spent` and `spent` rows drop out of the `status='active'` sum
regardless. Only a **partially** consumed pool pins the entitlement read — that
assertion is in `test_settlement_charges_only_the_overspend`, and it was
confirmed to fail against `SUM(minutes)`.

### 38.4 The admin container could not reach FreeSWITCH — found on first review

`/admin/telephony` Connect/Disconnect/Remove shipped broken and the DB half
would still have succeeded, which is worse than failing outright: the console
would report the number connected and it would answer nothing.

`lescall-admin` had none of the three things `lescall-api` has for exactly this
job — no `/etc/freeswitch` bind mount (so the write died with `PermissionError`
at the container root), no `group_add: ["994"]`, and no `telephony.env`, so
`_fs_reload()` had no `FS_ESL_PASSWORD` and fell through to `fs_cli`, which is
not installed in the app image. All three added to the admin service; recreate
with `sudo docker compose -f deploy/docker-compose.services.yml up -d admin`
(sudo because compose reads `/etc/lescall/admin.env`, root 0600).

Verified after the fix: `POST /admin/api/numbers/6/connect` →
`status: applied`, all three reload commands `ok` over ESL.

The general rule: any container that writes generated FreeSWITCH config needs
**mount + group 994 + telephony.env**. Two of the three fail silently-ish; the
mount is the only one that raises.

### 38.5 What the Add-a-number form CANNOT do

Adding a DID is not self-contained, and no form on this platform can make it so.
The console owns the platform half — trunk row → gateway XML, number row →
dialplan XML, agent bindings. The rest is at the carrier and is done in their
console:

1. the DID must be owned in the Plivo/Twilio account;
2. it must be attached to a trunk whose SIP domain matches `trunks.sip_domain`;
3. that trunk's origination URI must point at `sip:35.207.225.237:5060` (udp).

Miss any of those and Connect still reports `applied` — FreeSWITCH is correctly
configured to answer a call that will never arrive. So `applied` means "we are
ready for it", never "it works".

`provider_ips` is currently inert: **Kamailio is `inactive`** on this host and
FreeSWITCH is the SIP edge directly, so the carrier ACL those CIDRs feed is not
in the path. Every trunk has it blank and inbound works. That stops being true
the moment Kamailio is enabled.

### 38.6 Deploy

```bash
python -m shared.db.migrate          # 0025_admin_credit_grants
docker restart lescall-admin         # console + JSON API
docker restart lescall-workers       # the credits settlement pass
```

`lescall-api` and `lescall-runtime` need the restart too, once: both IMPORT
`shared/billing/entitlements.py` (the dial gate and the inbound E6 gate), and a
bind mount does not reload a module that is already in memory. Skip them and the
call path keeps summing `minutes` instead of what is LEFT of each pool — the
exact over-grant this shipped to prevent, live on the only surface that spends
the minutes.

```bash
docker restart lescall-api lescall-runtime   # no live calls first: `show channels count`
```

Tests: `tests/test_admin_credits.py` (24) + `tests/test_admin_telephony.py` (22),
both on the `<db>_test` Postgres rebuilt from the real migrations.

### 38.7 Proving a number works — the admin test call

`Connect` can only ever report that OUR side is configured (§38.5), so the
console gained the one action that tells "ready" apart from "working": ring a
phone FROM this number, over its own trunk, with its outbound agent live.
`/admin/telephony/<owner>` → **Place test call**, or
`POST /admin/api/numbers/{id}/test-call`.

It is a REAL call — metered, billable, counted against the tenant's quota — so
it goes through `check_dial_allowed()` like any other, and it releases its own
reservation in a `finally` rather than leaving a hold when the call is refused
or unanswered.

The originate string moved to **`shared/fs_originate.py`** to build it. That is
the point of the extraction: a second hand-written originate is how
`lescall_dest` goes missing on one surface only, and `lescall_dest` is what
stops the bridge falling back to "the most recently logged dial" — the §33 bug
that greeted 44 real people by a stranger's name. `server._fs_originate_cmd()`
now delegates to it and was verified byte-identical across four argument shapes
before the change went in; `test_outbound_identity.py` (16) still passes, and
`test_originate_carries_the_dialled_number` pins the channel variable.

### 38.8 Second handover: `+91 22 6423 1897`, milledger → rajbhatu4 (2026-09-23)

`support@milledger.com` → `rajbhatu4@gmail.com` (new account, put on growth by
the admin UI the same morning). Same three-row move as §38.1, driven through
`telephony_admin.create_number` → `transfer_number` → `connect_number` from
inside `lescall-admin`, with one twist: **the DID had no `phone_numbers` row at
all.** Trunk 6 and both `hardik-*` agents still sat under milledger and the
dialplan file was still on disk (last inbound answered 2026-08-07), but the
row itself was gone — so `resolve_agent_slug` had been falling through to the
founding-tenant legacy path for every call to that number, and the Transfer
button had nothing to act on. The fix is to *re-add* the number under the OLD
owner first (bound to its own trunk and agents), then transfer; a row created
directly under the new owner is refused because trunk 6 was not theirs yet.

Result: row 7 → `rajbhatu4@gmail.com`; trunk 6 renamed
`support-milledger-com_plivo-231897` → `rajbhatu4-gmail-com_plivo-231897`
(new gateway `UP`, old one `Invalid Gateway!` after `killgw`, file deleted);
agents 63/64 moved. Verified `resolve_agent_slug(did=…)` → `hardik-inbound` /
`hardik-outbound`, `_outbound_identity('rajbhatu4@gmail.com')` →
`own-number` via the new gateway, and milledger now has no number to call
from. Audit rows 25290–25292 (`ip='claude-code'`).

Not moved, as in §38.1: milledger's 32 calls, 10 contacts, 8 customer-memory
rows, 56 agent revisions (read by slug, so the new owner still sees history),
and one expired share link. Trunk 4 (`alwynantonyben…_plivo-231897`, Jul 12)
still claims the same DID on a different Plivo sub-account and its gateway is
still loaded — a leftover from the number's first life, untouched here.

Two operational notes. `docker exec` needs `-i` to feed a heredoc to
`python -`; without it the script "runs" with empty stdin and prints nothing,
which looks like success. And `connect_number` logs `kamailio ACL DB write
failed: unable to open database file` — Kamailio is inactive on this host
(§38.1), the FreeSWITCH half still reports `applied`, and that line is noise.

Same afternoon: the Prakhar agent (family `outbound_lead_qualifier`, ids 46/47,
display name ≠ slug) moved `angelo@dhronai.com` → `rajbhatu4@gmail.com` and
number 7 was rebound to it in both directions (`telephony_admin.bind_number`),
replacing Hardik, which stays in Raj's account unbound. "Activate" for a tenant
IS the number binding — `/api/agents/{name}/activate` is the admin-only
founding-tenant fallback and would not have routed this DID. Verified
`resolve_agent_slug(did=…)` and `_outbound_identity` both → the Prakhar pair;
angelo's number 4 (shubhangi) untouched. The pair's 53 calls and 11 revisions
stay attributed to angelo; the YAML files (tracked in git, unlike most agents)
did not change. Audit rows 25443–25444.

**Then the owner called the number and nothing happened — and nothing on this
host could have made it happen.** FreeSWITCH logged ZERO lines for the DID
that day; the last inbound INVITE for it was the sentinel's own loopback probe
on 2026-08-07 03:00, and the sentinel had been raising
`inbound-silence:2264231897` (1140 h) — plus `inbound-silence:8031339140`
(craftidea, 1180 h, loopback `USER_BUSY` every night) — while the platform's
own +91 80 6595 3160 passes every night. Manual probes 15:07 UTC: via Raj's
own gateway (`16584853209835561.zt.plivo.com`) → **403 CALL_REJECTED in
0.3 s** (that Zentrunk is the abandoned "previous default" from the 2026-07-03
topology note and rejects our INVITEs, so outbound is dead too); via
`plivo-outbound` → 183 then **486 USER_BUSY**, and with the DID as caller id →
`NO_ANSWER`; no inbound leg ever reached us. Both BYON-era DIDs went dark in
the same 2026-08-05…07 window, so this is a Plivo-side event, not a config
drift: the fix is in the Plivo console (number still rented? attached to an
inbound Zentrunk whose origination URI is ours, same as the working trunk
`68928093167632317`? and give trunk 6 a termination domain that whitelists
35.207.225.237). `applied` never meant "works" (§38.5). The probes created no
ledger or calls rows.

**Outbound half fixed the same day:** trunk 6 `sip_domain` re-pointed
`16584853209835561` → `25935838218519915.zt.plivo.com` (the termination trunk
angelo's trunk 5 already uses). Changing the domain of a gateway that is
ALREADY LOADED needs `sofia profile external killgw <name>` BEFORE
`connect_number` — `reloadxml`/`rescan` only add, so without the kill the
rescan sees the name and keeps the old proxy. The first self-call 43 s after
the re-create returned 503 `NORMAL_TEMPORARY_FAILURE`; 20 s later the same
call rang out (`NO_ANSWER`), identical to `plivo-outbound` with the same
caller id, so the carrier accepts the caller id and the trunk. Treat a 503
immediately after a gateway re-create as noise and re-test. Inbound is
still Plivo's to fix: the number rings but is never delivered to
35.207.225.237.

### 38.9 Third handover: `+91 22 6423 1897`, rajbhatu4 → patelashishk007 (2026-09-24)

Same day as §54, right after the Prakhar rebuild. Owner request: move the
number AND the Prakhar pair to `patelashishk007@gmail.com` (account created
09:36 UTC the same morning, already on growth with telephony). Run from inside
`lescall-admin` with zero live media connections: `transfer_preview(7, …)` →
`transfer_number(7, new, move_trunk=True, move_agents=True, rename_trunk=True,
actor="claude-code")` → `connect_number(7)` → two `shared.audit.record` rows
(25890 transfer under the old owner, 25891 connect under the new; `ip=
'claude-code'`). One transaction moved row 7, trunk 6 (renamed
`rajbhatu4-gmail-com_plivo-231897` → `patelashishk007-gmail-com_plivo-231897`,
sip_domain unchanged) and agents 46/47; FreeSWITCH wrote the new gateway XML,
rescanned, `killgw`'d the old one and deleted its file — all `+OK` over ESL.

Verified: `sofia status gateway patelashishk007-gmail-com_plivo-231897` →
`UP` (NOREG, IP-auth trunk); old name → `Invalid Gateway!`;
`resolve_agent_slug(did=…)` in `lescall-runtime` → `outbound_lead_qualifier-inbound`;
`server._outbound_identity('patelashishk007@gmail.com')` → `own-number` via the
new gateway, caller id 912264231897, agent `outbound_lead_qualifier-outbound`;
`_outbound_identity('rajbhatu4@gmail.com')` → "no number to call from" (correct:
Raj keeps only the unbound `hardik-*` pair). Bindings on row 7 unchanged, so
the tenant-side "activate" needed nothing.

Not moved, as in §38.1/§38.8: Raj's 3 Prakhar calls, 2 contacts and the pair's
agent revisions (read by slug). The Jul-12 `alwynantonyben…_plivo-231897`
gateway on another Plivo sub-account is still loaded — untouched again. The
runtime container's system `python` (not the host venv path) is what
`docker exec lescall-runtime` must use; a throwaway interpreter that imports
`conversation_call_bridge` prints the answer and then aborts at exit — the
live processes are unaffected (ports 9000/9001/9004 still bound, container
healthy).

---

## 39. One caller sentence, two agent replies (2026-09-09) — FIXED, NOT YET DEPLOYED

The caller says one thing in two or three pieces with a pause between them.
The agent answers the first piece, then answers the second piece — two
complete, separately-generated replies back to back for a single utterance.

### 39.1 The live evidence

`phase14_runtime/logs/docker/ws_audio_receiver.log`, call
`bff7a24a-9388-48f9-a007-f7cca09b0c63`, 2026-09-09 05:43:

```
05:43:21.495  [VAD] Dropping implausible speech span (5.70s measured vs 0.86s buffered)
05:43:22.131  fragment 2 'यह भी है नहीं।'            → its own turn
05:43:24.941  reply A generated  '…schedule करना चाहेंगे?'
05:43:24.942  fragment 3 'वहां पर स्टीम नहीं है मेनली।'  → its own turn
05:43:26.807  reply B generated  '…schedule करना चाहेंगे?'
05:43:27.980  reply A starts playing
05:43:27.982  reply B starts playing
```

Two near-identical pitches, queued 2ms apart. Same shape at 20:58:28 the
previous evening.

### 39.2 Why every existing guard was open

The continuation-merge layer (§11) exists for exactly this and did not fire.
Its gates all read the segment's speech span — and the span was gone.

`_flush_segment` carries a plausibility check: when wall-clock span disagrees
with *counted speech* by more than a second the span is not corpus-grade, so
it was zeroed. **But it zeroed both ends of it**, and a caller who pauses
inside a segment is *by definition* one whose wall-clock exceeds their counted
speech. The check therefore disarmed the anti-double-reply machinery precisely
for the callers it exists to protect:

- `transcript.speech_start_ts > 0.0` — required by the CONT-MERGE gate and by
  stale-fragment suppression. Both skipped.
- `_turn_txn_speech_end` is only advanced `if speech_end_ts > 0.0`, so it kept
  a value from **three turns earlier**. Fragment 3 measured its gap against
  speech that had ended ~9.8s ago — past even the 6s long window — and was
  committed as a brand-new turn.

One dropped span poisoned every turn after it.

### 39.3 The three repairs

1. **Only the ONSET is ever in doubt.** `_speech_end_ts` is derived from the
   flush moment minus counted silence; nothing the onset does can make it
   wrong. Keep it. Reconstruct the onset as `end − buffered speech` — the
   tightest honest lower bound, and exact when the caller did not pause — and
   set `AudioSegment.span_estimated` / `Transcript.span_estimated` so the
   ASR-replay corpus still publishes **nothing** rather than a guess. The
   corpus contract is unchanged; only turn-taking gained an anchor.
2. **`_turn_end_anchor()`** — a turn with no end timestamp anchors on *now*.
   Leaving the previous turn's value standing is the one option that must
   never happen; *now* is wrong by an STT round trip, the stale anchor was
   wrong by three turns.
3. **`_reply_still_inaudible()` merges regardless of the gap.** The gap
   windows exist to tell "the caller resumed their own sentence" from "the
   caller is answering the line we just spoke". While nothing has reached the
   caller there is no line to answer, so the second reading is impossible and
   the arithmetic does not need to be trusted. A reply still generating, still
   queued, or playing with nothing on the sink's playback clock has reached no
   ear — cutting it costs the caller nothing.

`_tts_playing` is **not** "heard": at 05:43:24.942 the TTS player had dequeued
reply A and begun synthesising, but the sink did not write its first audio
until 05:43:26.022. `playback_busy_until` is the real signal. A sink without
one (CLI `LocalSpeakerSink`) counts playing as heard — absent evidence, the
merge is the destructive option.

Two carve-outs kept: a reply the caller **cut** stays with the barge-in path
(it already preserves the pre-interruption words in the accumulator, and
`Task.cancel()` does not mark a task done immediately, so the moments after a
barge-in would otherwise read as "still generating"); and the rollback now
calls `sink.interrupt_playback()` before `tts.stop()`, same order as barge-in
— WAVs already injected into FreeSWITCH keep playing whatever we cancel
locally.

### 39.4 What did NOT change

Endpointing, the merge window, the hold/re-arm logic and the gap constants are
untouched — the owner's report was explicit that turn-end detection is right.
This changes only what happens to a fragment that arrives *after* the engine
has already taken the turn.

### 39.5 Tests

`tests/test_fragment_double_reply.py` (18). Each of the three repairs was
verified to fail before it: reverting them turns 5 of the 18 red. The VAD
tests drive the real `_process_vad_frame` on a clock that ticks one frame per
frame — the shared harness runs frames in a tight loop, under which a measured
span is meaningless.

Full backend suite: 1811 passed, 1 skipped.

### 39.6 Deploy

```bash
docker restart lescall-runtime     # voice path only; no live calls first
```

---

## 40. A dial that could never connect, and the phone book that fed it (2026-09-09) — ROOT-CAUSED + FIXED, LIVE

`craftidearesearch@gmail.com` pressed Call four times in two minutes. Every one
came back **"declined"**, which reads as "the person rejected the call".

Nobody rejected anything. The handset never rang.

### 40.1 What actually happened

`phase14_runtime` logs, then FreeSWITCH:

```
2026-09-09 07:23:38 INFO DIAL 7359800826 from 918031339140 (own-number) → declined
```
```
New Channel sofia/external/7359800826
Channel sofia/external/7359800826 entering state [terminated][404]
Hangup sofia/external/7359800826 [CS_CONSUME_MEDIA] [UNALLOCATED_NUMBER]
```

**SIP 404.** The carrier was asked to reach `7359800826` — a bare Indian
national number with no country code — and correctly said no such number
exists. Every dial that has ever connected on this box is `91…`:

```
DIAL 918866512292 from 918065953160 (own-number) → answered      # same minute
```

The tenant proved it themselves before we got there: they deleted the contact,
re-added it as `+917359800826`, and the call **answered at 07:28:01**. Same
person, same trunk, same agent, same minute of the day. The country code was
the entire difference.

`_parse_fs_result` maps `UNALLOCATED` into the same `declined` bucket as
`CALL_REJECTED`, which is why the UI could not tell the operator the one thing
that would have explained it.

### 40.2 Why a number without a country code was in the phone book

Three write paths, and only one of them was wrong:

| path | normalizes? |
|---|---|
| CSV / XLSX import (`_format_phone_digits` → `to_e164`) | yes |
| public API (`shared.contacts.upsert_contact` → `to_e164`) | yes |
| **dashboard Add-contact form** (`POST /api/outbound/contacts`) | **no** |

The form did `re.sub(r"[^\d+]", "", …)` and stored the result. `to_e164` — the
function that exists precisely to supply the country code an Indian national
number was typed without — was never called on the one path a human uses by
hand. `PATCH` had the same gap.

It was not one tenant. `support@milledger.com` had three contacts stored the
same way, all three marked `declined`, all three from this.

### 40.3 The fix is at the originate, not only at the form

Fixing the form would have left every already-stored row broken, and a phone
book is not a closed system — hand-typed rows, older imports, some future
surface. There are seven originate origins; normalizing at each is seven
chances to forget.

`shared/fs_originate.dialable_destination()` normalizes once, in the ONE
builder every real outbound leg funnels through (`originate_cmd`), and the
normalized digits go to BOTH the gateway dial string and `lescall_dest` — the
channel variable the bridge matches contact identity against (§33). Identity
matching is on the last 10 significant digits, so adding `91` cannot break it.

It repairs; it never breaks. A destination `to_e164` cannot place (under 7
digits, an already-international number) passes through as its own digits.
`shared/sentinel.py`'s loopback probe builds its own string and is untouched.

The write paths were fixed too (`_dialable_phone`, used by create AND update),
so the stored number matches what will be dialled.

### 40.4 Contacts → Edit could not change a phone number

Separate defect, same page. `frontend/src/pages/Contacts.tsx`:

* the Phone input carried `disabled={editor.mode === 'edit'}`;
* `saveContact` put `phone` in the body **only on create** — the edit branch
  PATCHed everything except the number.

So the dialog showed a phone field, accepted a correction, said "Contact
updated", and kept the old number. The server side has supported phone edits
all along ("*a typo'd number is the single most common reason to edit a contact
at all*") and Outbound's copy of the dialog sends it correctly. Only this page
was wrong.

### 40.5 Data repair

3 rows (`support@milledger.com`, ids 229/236/237) rewritten to `+91…`.
`sig_digits` is unchanged by construction, so nothing referencing them moved.
Not touched: 8-digit truncated imports and `+1800…` toll-free rows — those are
a different, older mess and guessing at them is worse than leaving them.

### 40.6 Scheduled calls and scheduled campaigns — NEW

Requested in the same session. The outbound surface could only act NOW.

* **`scheduled_calls`** (migration 0026) is the durable intent. lescall-api
  restarts on every deploy, so an in-process timer would silently drop every
  pending schedule each time we ship; only the poll loop lives in the process.
* **`shared/scheduling.py`** holds the decisions as pure functions over
  `(row, now)` — same split as `autodial.py`, testable without a clock or a
  phone.
* **Calling hours are re-checked AT FIRE TIME**, not only at creation. A row
  accepted for 18:55 and reached at 19:05 does not ring. Otherwise the
  scheduler would be the only way to get an out-of-hours call out of this
  system.
* **A schedule reached more than 30 minutes late is `missed`, never fired.**
  This is the guardrail that costs something to get wrong: after an outage,
  "fire everything that is due" is a burst of calls at whatever hour the box
  came back, to people who were promised a specific time. Late is worse than
  never — the customer already knows we said 4pm.
* **Rows are claimed with a conditional UPDATE** (pending → running, rowcount
  must be 1). One uvicorn worker today; the day there are two, SELECT-then-dial
  rings every scheduled contact twice.
* **A failed schedule is not retried.** The failure modes are "no minutes",
  "no trunk", "outside hours" — a blind retry fixes none and re-dials a real
  person with nobody watching. A row stranded `running` by a mid-dial restart
  is failed with an explanation, for the same reason.
* **No new guardrails, and no second copy of the old ones.** `outbound_dial`
  and `campaign_start` were reduced to thin HTTP wrappers over `_place_call`
  and `_campaign_launch`; the scheduler calls those. It is the seventh and
  eighth dial origin, and it inherits the duplicate guard, the voice-capability
  gate (V1), the billing admission (E1/E2), do-not-call and quota truncation by
  not having its own. `test_the_start_endpoint_is_only_a_wrapper_over_the_shared_launcher`
  fails if Start ever grows its own planning back.
* Refusals happen at CREATE time where possible — an out-of-hours slot, a
  do-not-call contact, an empty campaign audience — so the operator learns
  while looking at the form, not at 10am tomorrow.
* UI: one dialog and one list (`frontend/src/components/schedule.tsx`) shared by
  Contacts and Outbound. Times render in IST because the calling window is
  defined in IST; the browser's zone would show a Gulf operator a time the
  server refuses.

### 40.7 Tests

`test_scheduling.py` (27) + 10 schedule cases in `test_outbound_contacts_api.py`
+ 7 contact-normalization cases there + 3 in `test_admin_telephony.py`
+ 2 new wrapper-integrity cases in `test_campaign_guardrails.py`.
All server-importing suites: 130 passed.

### 40.8 Deploy

```bash
docker exec -w /home/angelo/lescall lescall-api python -m shared.db.migrate
docker compose -f deploy/docker-compose.services.yml up -d --build spa
docker restart lescall-api lescall-admin      # both use shared/fs_originate.py
```

---

## 41. Sir / Ma'am — gendered address from the contact sheet (2026-09-10) — SHIPPED

The agent could greet a contact by name (`{contact_name}`) and by time of day
(`{current_time}`). It could not address them by title, because nothing on the
call had ever read the one field that would allow it.

### 41.1 The gap was READ-side only

`contacts.gender` was not missing. It had been ingested since the phone book
existed, and every ingest path already normalized to `male` / `female`:

* CSV / XLSX upload — `_map_header_columns` matches a `Gender` or `Sex` header
  and runs `shared.contacts.normalize_gender` over the cell (`m`, `male`,
  `Man`, `F`, `Female` all land on two values);
* the public API — `POST /v1/contacts` accepts `gender`, documented in the spec;
* the dashboard — Contacts → Add/Edit has the field, and the list renders it
  as a badge;
* **exports already print it** — `Gender` has always been a column on the
  Contacts sheet of the selection workbook (`CONTACT_COLUMNS`).

So the answer to "do we take the field when providing Excel sheets?" was
already yes, both directions. What did not exist was a single reader on the
call path. The column was write-only for its whole life.

### 41.2 What was built: one placeholder, three links of plumbing

`{honorific}` joins `{contact_name}` and `{current_time}` as a token the engine
substitutes into an authored spoken line (`ConversationRouter._personalize`,
so it works in `response_bank.opening_greeting`, `outbound.opening_greeting`
and `call_flow.connect.intro_line` identically):

```
CSV "Gender" column ─► contacts.gender ─► call_links.gender  (migration 0027)
                                    │           │
                   (inbound: phone-book match)  │  (outbound: dial snapshot)
                                    └───────────┴──► set_call_context(contact_gender=)
                                                          └─► {honorific} + prompt band
```

* **Dial time** — `_record_dial` resolves and snapshots the gender beside the
  name it will be spoken next to. Resolved in `_record_dial` and NOT at the
  five call sites: only two of them (manual dial, campaign) hold a contact
  dict, so a parameter would have given the feature to some dial buttons and
  not others. Those two pass it directly and skip the lookup.
* **Call setup** — the bridge reads `gender` from the SAME verified dial record
  that supplies the name, so it cannot be borrowed from a different person's
  dial (§33). Inbound has no dial record, so it comes from the
  `_fetch_customer_context` contacts lookup that already runs — no new query.
* **Prompt** — `direction_block` gets the RESOLVED WORD, never the column
  value, so the model has no way to say "male" out loud.

### 41.3 The absent case is the feature, not the edge case

Measured on prod the day it shipped: **7 of 922 contacts carry a gender.**
99.2% do not, and never will — it is an optional column in an optional upload.
That makes "no gender on file" the dominant path, and it decides the design:

* `_honorific_word` returns `""` for anything that is not `male`/`female` —
  empty, junk, `prefer not to say`, an unmapped raw `M`. **It never guesses.**
  A wrong honorific on a live sales call is worse than no honorific.
* An empty result REMOVES the token and closes the seam it leaves
  (`_tidy_removed_token_seams`), so the same authored line works for both:

  ```
  "Good {current_time} {honorific}, am I speaking with {contact_name}?"
     male   -> "Good afternoon Sir, am I speaking with Rajesh?"
     female -> "Good afternoon Ma'am, am I speaking with Rajesh?"
     none   -> "Good afternoon, am I speaking with Rajesh?"
  ```

  Without the seam tidier the third line is `"Good afternoon , am I..."` with
  an audibly detached comma. That helper was extracted from
  `strip_unresolved_tokens`, which had the rules in the wrong order — dropping
  a dangling preposition left its own leading space behind
  (`"the guardian of ?"` → `"the guardian ?"`). Fixed while extracting.
* The prompt band adds NOTHING when the gender is unknown, so every existing
  agent's prompt is byte-identical. The golden-prompt fixtures did not move.

### 41.4 Script, not translation

Hindi and Gujarati carry their own everyday business honorifics, and an English
"Sir" dropped into a Devanagari greeting makes the TTS code-switch mid-sentence
— the same failure `_time_of_day_word` guards against. `hi` → सर / मैडम,
`gu` → સર / મેડમ. The prompt blocks pass the call's language explicitly; a
spoken line has no language field, so it falls back to the script of the
surrounding characters.

### 41.5 What is deliberately NOT a token

`{gender}` and `{sex}` do not resolve. They read as "insert the raw column
value", which would speak the word "male" to the caller; they hit the
unresolved-token backstop and are stripped instead.

`{Sir/Ma'am}`, `{salutation}`, `{sir_maam}`, `{gender_honorific}` and the usual
`${…}` / `{{…}}` / `[…]` shapes canonicalize to `{honorific}`; `{Mr./Mrs.}`,
`{mr_mrs}`, `{name_title}`, `{name_prefix}`, `{prefix}` and `{gender_title}`
canonicalize to `{title}` (see §41.8 — anything naming a "title" is the name
prefix, anything naming an honorific or salutation is the standalone word).

### 41.6 Surfaces that had to learn the new token

Each of these silently breaks the feature if missed:

* `agent_copilot_api._LIVE_TOKENS` — the platform-invariant lint. Without it,
  a greeting containing `{honorific}` is reported to the user as a defect.
* `_canonicalize_placeholders` — mirrors the engine regex. The two must agree,
  or the copilot writes a token the runtime strips.
* `spoken_surfaces` — the preview drives the real engine, so `{honorific}`
  would have RESOLVED AWAY in the dashboard's "what the next call opens with"
  line and told the author their placeholder was broken. It sets a sentinel
  gender and swaps the word back for the token, exactly as it does for the
  contact name.
* The copilot system prompt, the `connect.intro_line` schema docs, and the
  Copilot tip card.

The default new-agent greeting (`agent_builder_api._OUTBOUND_GREETING`) was
left alone on purpose — the feature is opt-in, and adding the token to every
new agent would be a product decision, not a plumbing one.

### 41.7 Tests

`test_honorific_placeholder.py` (45): the word itself, every unknown-gender
shape, both fallback languages, substitution with and without a gender, the
tolerated syntaxes, the raw-column tokens that must NOT resolve, the
call-context handoff both directions, dial-identity precedence over phone-book
memory, the prompt band's presence and absence, and preview resolution.
Plus the existing 70 greeting/context and 143 prompt-golden/invariant cases,
unchanged.

### 41.8 Follow-up same day: Mr./Mrs., and "everywhere in the agent"

Two gaps the first cut left, both raised by the owner within the hour.

**`{title}` — the name-prefix form.** "Sir" stands alone ("Yes Sir"); "Mr."
only ever precedes a name ("Mr. Rajesh"). They occupy different slots in a
sentence and an author needs both in the SAME sentence, so `{title}` is a
second token rather than a smarter first one. Same optionality contract, same
localization: `hi`/`gu` → श्री / श्रीमती (શ્રી / શ્રીમતી).

Disambiguation rule, so an author never has to guess: anything naming a
**"title"** is the name prefix; anything naming an **honorific / salutation /
sir / ma'am** is the standalone word. `{gender_title}` therefore MOVED from
`{honorific}` to `{title}` in this pass (shipped hours earlier, no known users).

> **`Mrs.` asserts a marital status a spreadsheet phone number does not tell
> us.** `Ms.` carries the same respect and asserts nothing. The owner asked for
> Mrs. and it is what Indian business calling expects, so that ships — but it is
> the single named constant `_FEMALE_NAME_PREFIX_EN`, so the neutral form is a
> one-word change, not a hunt through a table.

**"All such things in the agent."** The first cut resolved placeholders in
exactly two surfaces (`_personalize`: the opening greeting and the connect
intro line). Every OTHER authored spoken line — closings, canned phrases,
objection answers, identity responses, graceful close, wrong-number and
wrong-person lines, handoff, timeout fallbacks — funnels through
**`_lang_response`**, which did no substitution at all: an owner writing
"Thank you {honorific}" into their closing got the token STRIPPED and spoken as
"Thank you". Substitution moved there, so gendered address is a property of the
AGENT rather than of the greeting field.

That required splitting the old `_personalize` in two, and the split is
load-bearing:

* `_substitute_tokens` — resolution only. This is what `_lang_response` calls.
* `_personalize` — `_substitute_tokens` + `strip_unresolved_tokens`, for the
  surfaces that hand a finished line straight to TTS.

**Never move the strip backstop into `_lang_response`.** `objection_playbook`
answers are handed `{trust_proofs}` by `_match_objection_playbook` AFTER
`_lang_response` returns; stripping there would delete it. There is a test
pinning exactly this.

Finally, the prompt band now gives the model BOTH resolved words with a worked
example and "for the WHOLE call, not just the greeting", so its own improvised
sentences carry the address too — the placeholders only cover lines the owner
wrote by hand. `prompt_blocks._resolve_author_tokens` also resolves the tokens
in the two owner lines QUOTED into the prompt as scripts
(`identity_confirm_line`, `wrong_person_response`), because SCRIPT FIDELITY
tells the model to reproduce a quoted line word for word and it has previously
reproduced punctuation it should not have (§37).

Tip card rewritten short: names the four tokens, one example line using all
four, one caveat sentence. It is a discovery hint, not documentation.

Tests: `test_honorific_placeholder.py` 45 → 95.

### 41.9 Deploy

Migration first — the bridge tolerates a missing column, the INSERT does not.

```bash
docker exec -w /home/angelo/lescall lescall-api python -m shared.db.migrate
docker compose -f deploy/docker-compose.services.yml up -d --build spa
docker restart lescall-api        # _record_dial + copilot
docker restart lescall-runtime    # bridge + conversation engine
```

## 42. Who is addressed, and who is speaking — gender as DATA for every agent (2026-09-11) — SHIPPED

Owner mandate, platform-wide, existing agents and every future one:

1. **The caller is addressed by their contact record.** Sir/Ma'am and
   Mr./Mrs. come from `contacts.gender`. With no gender on file the agent uses
   **no gendered address at all** — not through `{honorific}`, not through a
   script that hardcoded "Sir", not through the model improvising one.
2. **The agent speaks in the gender of the voice the business selected.**
   Pronouns and first-person agreement ("kar raha / rahi hoon") follow the
   TTS speaker; there is no separate pronouns setting any more.
3. **Copilot reminds the user** whenever they ask for Sir/Ma'am in a script
   that it is dynamic and only works for contacts that carry a Gender.

### 42.1 What was actually happening (measured on prod, 2026-09-11)

| Defect | Evidence |
|---|---|
| Women greeted "Sir" | jai_mistry-2 (craftidearesearch@gmail.com): "kya main SHAHANAZ Sir…", "SHREE Sir" — both contacts marked **female**; call 805 even logged `honorific="Ma'am"` and still said "Sir", because a hardcoded verbatim line beats the prompt band |
| The model guesses titles | maya_lescall_sdr: "Mrs. Paul" then "Mr. Paul" to the same caller; "Mr. Raj" to a contact with no gender |
| The name fallback WAS gendered address | `{contact_name}` with no name spoke the literal "sir/ma'am": "Am I speaking to sir/ma'am?" on 29+ lines (kaapd_pooja 16, vishal_patel 10, hardik, shubhangi) |
| Male voices told to be women | 7 shubh-voiced agents carried `pronouns: she` (several by schema default alone); pramukh_agent: "मैं Pramukh Group से **विशाल पटेल** बात कर **रही** हूँ" |
| Female voice, masculine script | jai_mistry-2 switched to simran by its owner's own Copilot prompt; the script stayed "samajh gaya / chahta tha", the prompt said "ALWAYS rahi" — the persona flipped gender mid-call (follow-up 765: caller "confused about the agent's identity") |
| Platform lines | conversation.py's own hi/gu lines ("जी, समझ गया — … सकता हूँ?", "હું નોંધી રહ્યો છું") were masculine and spoken by every female agent |
| New agents | Copilot defaulted every unvoiced agent to a FEMALE speaker — the Kaapd brief's "करण बोल रहा हूँ" got priya |
| Stage goals | `{honorific}` was never resolved in a stage goal (§41 gap) — and stage goals are where Copilot-built agents keep their verbatim lines (Jai Mistry: 20 of 54 "Sir"s) |

### 42.2 The design: one module, two rules, every exit

`calling-agent/backend/app/voice/address.py` — pure functions, shared by the
engine output, the prompt builder, the spoken-surface preview and the Copilot:

* `enforce_address(text, gender, contact_name, protected)` — held to the
  contact: known gender → the right word **in the word's own script**
  (Sir→Ma'am, सर→मैडम, Mr.→Mrs.); unknown → the word is removed and the seam
  closed ("Achha Sir, samajh gaya" → "Achha, samajh gaya"; a clause whose only
  object was the word is dropped). Titles are only touched **in front of the
  contact's own name**; names the agent's script titles itself ("Mr. Umang,
  the CEO" — dhronai_ea) are protected third parties.
* `regender_self(text, gender)` — first-person agreement to the voice.
  **Every rule is anchored on a form only the speaker can own** ("… hoon",
  the -ūṅgā future, a clause opening with "main"/"मैं", Gujarati "… છું"),
  and uses explicit word lists, never suffix rules — female names end in -a and
  -ta ("main Kavita hoon"), "number lag gaya hoga" / "wajah rahi" / "kya hua"
  are not first person, "lungi" is a garment and "આવી" means "such".
  Measured: 6,119 recorded agent lines → 35 changes, all in the three known
  mismatched agents, zero false positives.
* `resolve_contact_name(text, name)` — no name on file: an identity-question
  clause is dropped ("Hello, am I speaking with {contact_name}?" → "Hello."),
  elsewhere the token simply goes. If an opening is left empty the engine
  opens with the composed identity intro (`_composed_intro_line`).
* `agent_gender(agent_def, lang)` — the voice the caller HEARS for that
  language (same chain as the bridge: that language's voice → default
  language's → shubh); stored pronouns only for an unclassifiable speaker.

**Script-aware word boundaries are load-bearing.** `\b` is useless around
Devanagari/Gujarati (combining marks are not `\w`): `\bसर\b` matches inside
सर्विसेज / अक्सर / फ्रीलांसर and `\bસર\b` inside ખૂબ સરસ / સરખામણી — all five
were in live agent files. The danda (।) sits inside the Devanagari block; the
first cut counted it as a letter and every sentence ending "…रही हूँ।" was
invisible to the rules.

### 42.3 Where it runs (the engine)

* `ConversationRouter._finalize_spoken` = enforce + regender. Applied at the
  **choke points**, not the 40 yield sites: a `process_stream` wrapper over
  `_process_stream_raw` (a chunk the rule empties is dropped — blank text would
  make the pipeline say "Sorry, could you repeat that?"), `generate_opening`,
  the three watchdog lines and `connect_onset_line` (thin `_raw` wrappers), and
  `ConversationMemory.spoken_filter` so **history records what was said** — a
  model shown its own "Sir" to a woman says it again.
* `_personalize` finalizes too; `_substitute_tokens` stays substitution-only
  (the §41 `{trust_proofs}` rule is untouched).
* Prompt: the **stage goal** and every quoted `outbound.*` line go through
  `_render_author_line` (tokens resolved, address enforced, self-gender
  applied) — the model is told to copy quoted lines verbatim, so whatever is
  quoted is what gets said. The STATIC band (knowledge text, objection
  answers) is per agent × language and cannot know the caller: gendered
  tokens are removed there (`neutral_address`) and regendered to the voice —
  cache-safe, because the voice is static per agent × language.
* `_address_lines` with no gender now says **"NEVER call them Sir, Ma'am,
  Madam, Mr., Mrs. or Ms. … use their name, 'ji', or 'aap'"** — the §41
  "add nothing" contract is reversed on purpose (the model filled the silence).
  For outbound it renders with or without a contact name.
* `pronoun_instruction(agent_def, lang)` derives from the voice.
* `AgentDefinition.from_dict` syncs `identity/personality.pronouns` to the
  voice on every load (so every save persists it), and `store.SERVER_MANAGED_PATHS`
  now includes both — **without that, a Copilot voice change would have been
  rolled back by the scope proof** as collateral.

### 42.4 Copilot

* Literal address in SPOKEN fields → placeholders
  (`address.tokenize_spoken_fields`, the same walker the migration used) — on
  the **change subtree** in `_normalize_definition` and in
  `update_stage`/`add_stage`, never on the merged agent (a scoped write that
  only moves the voice must not rewrite goals it never touched). Rules,
  caller-side patterns and knowledge prose are never tokenized.
* **Gender-data reminder, guaranteed:** when the user's message asks for an
  honorific in the script (`_asks_for_honorific` — an honorific word AND an
  ask about what the agent says, so "ok sir, do it" does not trigger) and the
  reply does not mention gender, the server appends the reminder **with the
  account's real coverage** ("Right now 3 of your 3 contacts have a gender on
  file").
* Default voice for an unvoiced new agent follows the first person the owner
  WROTE (`authored_self_gender` — "बोल रहा हूँ" → male), female only when the
  script commits to neither. Evidence from the script, never from the name.
* Doctrine rewritten: placeholders resolve inside stage-goal verbatim lines;
  no "sir/ma'am" fallback; a fixed Sir does not exist; the agent's gender
  follows its voice and there is no pronouns setting.

### 42.5 Ingest, Builder

* `shared.contacts.normalize_gender` also maps पुरुष/महिला/स्त्री, પુરુષ/સ્ત્રી,
  Mr/Mrs/Ms/Miss, Gents/Ladies. Anything else is stored as written and never
  addresses.
* Builder: the Pronouns dropdown is read-only ("Follows the voice"). The
  Copilot tip card says a contact with no gender is addressed by name only.

### 42.6 Agents whose persona now follows their voice

serene_health is named **Priya**, its script is feminine, but its voice is
**shubh** — it now speaks as a man. The voice is almost certainly the mistake:
switch it to a female speaker. jai_mistry-2 is the mirror (Jay Mistry,
masculine script, simran by the owner's own request) — it now speaks as a
woman consistently; its name still says Jay.

### 42.7 Tests

`test_address_self_gender.py` (95) — both regender directions, 13
not-first-person traps, every address case incl. third parties, lookalike
words and the danda, the no-name rule, tokenization scope, voice derivation,
the schema sync, and the engine end to end (openings for each kind of contact,
the stream wrapper, history, platform lines, the rendered stage goal).
`test_honorific_placeholder.py`: the three "unknown gender says nothing"
tests were rewritten to the new contract. `test_jai_mistry_no_focus_convergence`
compares against the goal as rendered for the caller. Golden prompts: exactly
two line changes in all 90 fixtures (pramukh he/him; the no-guess line).
`test_scheduling::test_a_settled_schedule_cannot_be_moved` fails on Fridays
and Saturdays — `_soon() + 1 day` lands on Sunday; pre-existing, unrelated.

### 42.8 Deploy + fleet migration (done 2026-09-11 ~07:08 UTC)

```bash
docker compose -f deploy/docker-compose.services.yml up -d --build spa   # Builder field, tip card
docker restart lescall-api        # Copilot + shared/contacts
docker restart lescall-runtime    # engine (0 live media sockets at restart)
# THEN the migration — after the engine can resolve tokens in stage goals:
set -a; . deploy/.pg_env; set +a     # LESCALL_DB_URL, or the store writes SQLite
python migrate_address.py --apply    # session scratch script; logic = address.tokenize_spoken_fields
```

15 agents written through `store.write_agent` (snapshot + scope proof), actor
`platform:runbook-42` — `SELECT * FROM agent_revisions WHERE actor='platform:runbook-42'`
lists every pre-image for rollback. 4 Jai Mistry agents: 34 spoken fields each
→ `{honorific}`/`{title}`. 11 agents: stored pronouns synced to their (male)
voice — demo_realty, nintendo_gamer, pramukh, pramukh_agent, sen_studio ×2,
serene_health, test_nam ×2, uuu ×2. A second dry run writes 0 (idempotent);
the runtime registry watcher hot-reloaded all 15. Verified inside the running
runtime container: jai_mistry-2-outbound greets SHAHANAZ/SHREE "Ma'am", JAY
"Sir", a gender-less contact by name only, all in "rahi hoon" (simran), and
pramukh_agent's Vishal Patel says "बात कर रहा हूँ".

---

## 43. Voice LLM → gemini-3.5-flash-lite, and why the prompt never cached (2026-09-11) — SHIPPED, LIVE

**Why.** Google's August 2026 invoice for this project was ₹5,705.85, and ₹4,418
(77%) of it was live calls on `gemini-3.5-flash`. ₹3,997 of that was **input**
tokens: the ~4.9K-token system prompt is re-sent on every exchange, and only 7%
of it was billed at the cached rate. The owner asked for an ~80% cut.

### 43.1 Why §27's cache-ordered prompt never cached

§27 banded the prompt STATIC → PER-CALL → PER-TURN against a **1,024-token**
floor (Gemini 2.5 Flash's). Gemini's docs (ai.google.dev/gemini-api/docs/caching)
put the implicit-cache minimum for **3.5/3.6/3.7/3.8 Flash at 4,096 tokens**.
On 161 consecutive-turn pairs of real Wellfound calls the byte-identical
opening was ~3,930 tokens (median) — **0 of 161 reached 4,096**. It ends at
`━━ CALL STATE ━━`'s `Turn: N` line, the first thing that changes every turn.

Moving every remaining call-stable section (SCRIPT FIDELITY etc.) above it
lifts the opening to ~4,200 tokens. Measured on a 17-turn replay, paced like a
call: 3.5-flash went 0% → **12%** cached (5 turns hit, ~2,000 tokens each);
3.5-flash-lite and 3.1-flash-lite stayed at **0%** in both layouts. Implicit
caching is best-effort, so reordering was NOT shipped — it would also undo
§36's deliberate recency placement of SCRIPT FIDELITY for a few percent.
Explicit `cachedContents` now works on this key (billing is on — the startup
`[PROMPT-CACHE]` probe reports AVAILABLE), and the OpenAI-compat endpoint
accepts `extra_body={"google": {"cached_content": ...}}` — that is the
reliable next lever (untested on the call path).

### 43.2 The switch

`LLM_MODEL=gemini-3.5-flash-lite` (effort stays `minimal`; 3.5-flash-lite
REJECTS `none`). Evidence, same captured requests for every model:

| | 3.5-flash (was) | **3.5-flash-lite** | 3.1-flash-lite |
|---|---|---|---|
| $ / 1M in · cached · out | 1.50 · 0.15 · 9.00 | **0.30 · 0.03 · 2.50** | 0.25 · 0.025 · 1.50 |
| Blind judge, 49 real turns (gemini-3.1-pro-preview) | 5.92 / 10 | **7.10** | 6.43 |
| Top-or-tied | 18 / 49 | **29 / 49** | 18 / 49 |
| TTFT p50 (172 turns, from this VM) | 1.21 s | **0.98 s** | 0.86 s |
| Words / reply | 21.2 | 20.2 | 24.7 (truncated a Hindi reply at max_tokens) |

Hindi + Gujarati smoke on the Pramukh golden prompts (5 turns): all in the
caller's language; romanized Hindi on one turn, which the prompt allows.
**Known 3.5-flash-lite weakness:** 3 of 49 judged turns re-asked the
verbatim RELOCATION_CHECK script after the caller had already answered it.
A bare `[END_CALL]` reply (1/49) is already covered — the engine speaks the
closing line (`conversation.py`, "never end on dead silence").

**Projected at August volume:** live-call LLM ₹4,418 → **~₹914 (−79%)**;
the whole Gemini bill ₹5,706 → ~₹2,200, because Copilot, the call
analysis and the (one-off) smart-filter testing are unchanged.

### 43.3 Also fixed

- `shared/ai_pricing.py`: 3.5-flash-lite priced ($0.30/$0.03/$2.50) and
  3.1-flash-lite's cached rate added — otherwise every call after the switch
  would have reported as unpriced. Rates match the August invoice to the cent
  at **~₹95.65/$**; `AI_USD_INR` still says 88.33, so ₹ estimates run ~8% low.
- `prompt_cache.probe_cache_capability` sent the Gemini key as `?key=`, and
  httpx logs request URLs at INFO: the live key was written to
  `ws_audio_receiver.log` on every runtime start (8 lines across `.log` and
  `.log.1`). The key now rides in `x-goog-api-key`. **Rotate the key.**

**Rollback:** `LLM_MODEL=gemini-3.5-flash` in `calling-agent/.env`, then
`docker restart lescall-runtime lescall-api`.

**Open:** the live-call fallback is dead — `gpt-4o-mini` on OpenAI, whose
account has no credits (429). A Gemini fallback needs a code change first: the
fallback `create()` omits `reasoning_effort`, and Gemini 3.x truncates without it.

---

## 44. The free tier had no ceiling on anything (2026-09-12) — SHIPPED, LIVE

**Reported:** "People in free trial are able to make more than one agent. I
want a strict restriction on the same… Also a token limit so they do not spam
messages copilot. Also, implement a cybersecurity measure on the SQL injection
through copilot or anywhere."

Three things, one root cause for the first two.

### 44.1 Why every cap was inert

The caps were all there. `plans.max_agent_families` is 1 for `free`;
`check_agent_create()`, `check_copilot_turn()` and `_billing_claim_guard()`
all read it; the SPA even has a disabled-with-a-reason "New Agent" button.
None of it ran, because every gate opens with:

```python
if not enforcement_enabled():
    return Decision.ok()
```

and `settings(owner='', key='billing_enforce')` has been `{"enabled": false}`
since 2026-08-01, on purpose — turning it on starts refusing phone calls on
minute quotas, locking accounts and hiding paid features platform-wide. That
is a business decision, and it is not the one that was asked for.

So the switch is still off. What changed is that a small set of limits no
longer asks it. **The trial floor** (`TRIAL_*` in
`shared/billing/entitlements.py`) is an ANTI-ABUSE floor that binds for `free`
tenants whatever the switch says:

| | free (floor) |
|---|---|
| agent families | **1** |
| copilot messages / day | 20 |
| copilot tokens / day | **150,000** |
| copilot tokens / message | **25,000** |

It only ever TIGHTENS (`_floor()` is a `min`, and NULL means unlimited, so it
is the loosest value — not the tightest; getting that backwards hands a trial
account an unlimited budget and looks like a typo in review). Paid tiers are
untouched while the switch is off, and a locked account is still only enforced
when the switch is on — locking is an administrative act, not an abuse
control, and must not start biting because of this.

Scope is `free` ONLY. **Pilot is a PAID trial** (Rs 2,999 / 7 days) and keeps
the 3 agents it was sold; capping it would be a pricing change wearing a
security fix's clothes.

`is_trial()` reads the RESOLVED plan, which catches the two cases a
`plan_code == 'free'` comparison at the call site would miss: an account with
no `entitlements` row at all (a signup from thirty seconds ago —
`_free_defaults()` stamps `plan_code='free'`), and a Pilot whose window
elapsed, which `get_entitlements()` demotes to Free on READ.

**No tenant was broken by this.** Every `free` owner held exactly 1 family at
ship time; the two multi-agent tenants are Growth and Enterprise.

### 44.2 The agent cap has two gates and BOTH needed the change

`_assert_agent_quota()` is the pre-check, before any filesystem write.
`_billing_claim_guard()` is the backstop, inside the same transaction as the
`INSERT INTO agents`, under `pg_advisory_xact_lock` — because a bare
count-then-write is TOCTOU and two concurrent creates on a cap of 1 both read
0 and both succeed. The guard had its own `if not enforcement_enabled():
return`. Fixing only the pre-check would have left the cap one race away from
bypassable, which is not a cap. Both now route through `agent_cap()`.

Paths covered: `POST /api/agents`, the twin creator, the Copilot's
`create_agent` tool (it calls the builder handlers), and share IMPORT
(`agent_share_api` -> `_assert_agent_quota`).

### 44.3 The copilot counted send presses, not work

`copilot_turns_per_day` counts USER MESSAGES. One message runs a server-side
agentic tool loop of up to `_MAX_TOOL_ROUNDS` (20) full LLM requests, each
carrying the system prompt, the tool schemas and the fitted history — ~11.5k
input tokens by the primary model's own budget, plus up to `LLM_MAX_TOKENS`
out. **Twenty polite messages and twenty adversarial ones are the same number
to that counter and two orders of magnitude apart in cost.** And the copilot's
model quota is SHARED with live phone calls (`COPILOT_LLM_TPM` plus the
provider's daily ceiling), so the cheapest account on the platform could
degrade every paying tenant's calls at zero cost to itself.

There was no token accounting at all: `_chat_completion()` returned
`completion.choices[0].message` and dropped `completion.usage` on the floor.

Now:

* **`_charge_tokens()` runs inside `_attempt()`**, not at the call site — a
  nudged `tool_use_failed` retry and a walk down the fallback chain are
  additional real requests, and a budget counting only the successful one lets
  a tenant provoking malformed tool calls spend three times what it records.
* **The provider's `total_tokens` is trusted over `prompt + completion`.** On
  Gemini the parts do not add up: reasoning tokens are billed at the output
  rate and folded into the total without appearing in `completion_tokens`
  (§43, and the 2026-09-12 attribution note — ~29%). Summing the visible
  numbers under-counts the expensive half of every request. `max(total,
  parts)` so a nonsense total cannot undercount either.
* **Three ceilings, and they stop different things.** Turns/day stops 10,000
  one-word messages. Tokens/day stops 20 enormous ones. The PER-MESSAGE
  ceiling is the only one that can stop a single 400k-token paste — the daily
  budget cannot, because by the time the counter moves the request has already
  been sent and the shared quota already spent.
* **A mid-turn guard bounds the overshoot.** The daily budget is checked once,
  before the turn, so a tenant one token short could still spend twenty
  rounds. The remaining budget is fetched ONCE per turn and drawn down locally
  from the usage each round already reports — no DB read per iteration — and
  the loop breaks when it runs out, keeping whatever the tools already saved.
  Raising there would discard completed work the tenant was charged for.
* Counters are best-effort in one direction only: **a dropped count costs the
  platform tokens; a raised exception costs the user their reply after the
  model already ran.** Every read degrades to 0 and every gate fails open.

Storage is `copilot_token_usage(owner, day, …)` — one upserted row per owner
per IST day (migration 0028). NOT `audit_events`: the turn cap can count rows
because a turn IS a row, but tokens are a SUM, and summing a JSON `meta`
column over a day sits in the latency of every chat message. Same IST midnight
as the turn cap — two budgets resetting at different midnights is the kind of
detail that becomes a support ticket nobody can reproduce.

`refusals` on that row is for the operator view: a tenant at the ceiling with
12 requests is building, one at the ceiling with 900 is not.

### 44.4 The meter has to agree with the gate

`usage_summary()` now returns the FLOORED caps, not the raw plan row, plus
`trial`. The SPA hid every cap behind `enforcement`, so a binding trial cap
was invisible right up to the 402 — the user filled in the whole form and met
an unexplained refusal on submit. `AgentsList` now reads `enforcement ||
trial`, and Billing draws an "AI assistant today (resets midnight IST)" meter.
The copilot allowance is spent by a server-side tool loop, so a user has no
way to feel it being consumed; without a meter the first sign of it is a
refusal mid-build.

Refusal copy is trial-aware. "Your plan includes 1 agent" reads as broken to
someone who never chose a plan — it is "The free plan includes 1 agent and you
have 1. Archive it to build a different one, or upgrade for more.", and the
race path (`_billing_claim_guard`) emits the SAME sentence, because a user who
loses the race must not be told something different.

`CODE_QUOTA_COPILOT_TOKENS` is separate from `CODE_QUOTA_COPILOT` on purpose:
collapsing them would report an 8-message day as "you have used today's 20
messages", which is simply false.

### 44.5 SQL injection — the audit found none, so the work was to keep it that way

**Audit result: no injectable query.** Every user-controlled value rides a bind
parameter. The f-string sites that exist interpolate an IDENTIFIER chosen from
a fixed whitelist in the same module (`_SORTABLE.get(sort, "c.started_at")`,
`SET {field}=?` over a literal field tuple, `ci.{field}` over a literal jsonb
column tuple). `_search_clause()` even escapes LIKE wildcards so a typed `%`
matches itself. The Copilot barely touches the DB directly — its tools call
builder functions, which parameterize.

That is a good state and an unstable one: the next `ORDER BY
{whatever_the_user_typed}` looks exactly like the safe ones at a glance.
So the deliverable is a gate, not a patch.

**`tests/test_sql_safety.py`** walks the AST of every non-test `.py` in the
repo and flags any `execute()` whose SQL is built by f-string, `+`, `%` or
`.format()`, unless each interpolated expression is PROVABLY fixed in the
source (a literal; a module-level `UPPER_CASE` constant; arithmetic over
those; a ternary whose every branch is safe; `",".join("?" for _ in x)`, whose
alphabet does not depend on `x`; a comprehension over a proven-constant
iterable; a local traced back to any of the above, resolved in the ENCLOSING
scope only). Everything else must appear in `_REVIEWED` with a written reason
— 27 entries, every one read during this audit. A name the scanner cannot
trace is UNSAFE: the unknown direction is the refusing direction.

A second test prunes `_REVIEWED` entries that no longer match a query, so the
list stays a description of the code rather than a pile of past exceptions.
Verified by planting all three injection shapes and watching it fail.

**Two real defects were fixed in `shared/db/database.py`:**

* `sql.replace("?", "%s")` rewrote question marks INSIDE STRING LITERALS.
  `WHERE label = 'why?'` became `'why%s'`, which psycopg then reads as a
  placeholder — so the statement either raises a count mismatch or, worse,
  **silently consumes the next bind value in the wrong position and runs a
  valid query against the wrong data.** Replaced with a scanner that skips
  single/double quotes (doubled-quote escapes included), dollar-quoted bodies,
  `--` comments and NESTED block comments. `%` is deliberately untouched —
  callers already write a doubled `%` (the pg_trgm operator in
  `intelligence/search.py`) and doubling them again here would break it.
* **A NUL byte in any string parameter 500s Postgres** (`DataError: PostgreSQL
  text fields cannot contain NUL bytes` — confirmed live). A 500 any caller
  can trigger at will (a percent-encoded NUL in a query string, a JSON-escaped
  one in a body field, a control byte in an uploaded CSV) is a denial of
  service with extra steps. It was closed for `/v1` at its own boundary
  (`public_api/sanitize.py`, §30) and left open for the dashboard's `/api`,
  CSV upload, the intelligence pipeline and every worker. Now stripped **at
  the psycopg adapter** — the one place every writer passes through, HTTP or
  not, so a new endpoint or worker cannot forget to opt in and a middleware
  could not have covered the non-HTTP writers at all. Only NUL, only from
  `str`; `bytes` are untouched (a NUL in binary is legitimate), and the wider
  control-character hygiene stays a product decision at the API boundary.

Also: `intelligence/search.py::_tune_vector_scan` interpolated an env string
into `SET LOCAL hnsw.iterative_scan = …`. A GUC takes a literal, not a bind
parameter, so the interpolation has to stay — but the value is now matched
against `_ITERATIVE_MODES` (and `ef_search` clamped through `int()`), so an
operator typo falls back to the default instead of being pasted into SQL.

### 44.6 Deploying and reverting

```bash
python -m shared.db.migrate            # 0028
docker restart lescall-api lescall-runtime lescall-workers lescall-admin
docker compose -f deploy/docker-compose.services.yml up -d --build spa
```

`shared/db/database.py` is on the voice path, so the runtime restarts too.

To relax the floor for one tenant without a code change, give them an
entitlement row on a non-`free` plan, or an admin override —
`effective_limits()` reads the resolved plan, so the floor stops applying the
moment they leave it. `TRIAL_*` in `shared/billing/entitlements.py` are the
numbers; changing a plan's `copilot_tokens_per_day` is a data change.

**Tests:** `test_trial_floor.py` (15) + 5 new in `test_copilot_llm_budget.py`
+ `test_sql_safety.py` (2). `test_free_plan_cap_binds_with_enforcement_off` is
the one that matters — it asserts enforcement is OFF first, so a run that
silently had it ON (and therefore proved nothing) cannot pass.

**Pre-existing, unrelated, still failing:**
`test_copilot_edit_engine.py::test_propose_apply_rollback_scoped` and
`test_store.py::test_write_agent_round_trip_and_revisions` — both fail
identically on a clean tree (revision counter, not billing).

---

## 45. The Masters-Naukri calls — seven defects, six of them platform-wide (2026-09-16) — ROOT-CAUSED + FIXED

**Reported** (shubhangi inbound + outbound, group "Masters Naukri"): questions
repeated, an agent that "always says *am I speaking to Piyush Gupta **or**?*",
a caller offered a hang-up after saying their employer's name, an inbound
caller asking about a role told "I'll call you back in about 20 minutes" and
hung up on, a garbled "immediate joiner" restated as "joining in May", and
long delays with the agent talking over the caller.

Everything below is fixed in the ENGINE. No agent YAML changed, so it applies
to every existing agent and to every agent Copilot builds next.

### 45.1 "Am I speaking to Piyush Gupta **or**" — the TTS cache froze a hallucination

The generated text was clean. `ws_audio_receiver.log` shows exactly one TTS
request, `'Am I speaking to Piyush Gupta?'`, and the transcript records
exactly that line. The **audio** is where the "or" lives.

Isolating the agent's speech out of `call_audio.wav` (the caller channel is
silent across 3.5–6.2s) and running it back through Sarvam STT:

```
3.45–5.10s  ->  'Am I speaking to Piyush Gupta?'
5.15–6.25s  ->  'or'                      # 0.9s, separated by a 150ms gap
```

Re-synthesizing the same text at the same model/speaker/pace returns 1.3–1.8s
of clean audio, six samples in a row. The live one was **2.69s** — and
`[TTS:Sarvam] Cached utterance (2.7s, 42974 bytes)` says it went into the
canned-audio disk cache, whose key includes the resolved `{contact_name}`.
**That is the "always"**: one bad synthesis, then a cache hit on every
subsequent call to that contact, forever, with no API call to correct it.

Auditing the 199 cached per-contact greetings against a duration model found
three outliers. Two are confirmed hallucinations:

| cached line | stored | STT of the stored audio |
|---|---|---|
| `Am I speaking to Piyush Gupta?` | 2.69s | `Am I speaking to Piyush Gupta or` |
| `Am I speaking to Yogesh Kumar?` | 3.03s | `Am I speaking to Yogesh Kumar 40, the man is Kumar?` |

**Fix — audio must be plausible for its text** (`tts_sarvam.py`):
`_expected_speech_seconds()` models duration as per-sentence pad + per-character
rate (Indic characters counted double). Calibrated over 88 live Bulbul
syntheses across en/hi/gu, both speakers, paces 1.0–1.3, 5–129 characters: the
worst LEGITIMATE ratio measured **0.95**; the two hallucinations measured
**1.27** and **1.44**. The gate rejects above `TTS_DURATION_MAX_RATIO` (1.2).

- **write gate** — an implausible utterance is never cached.
- **read gate** — a stored entry that fails is deleted and re-synthesized, so
  the ~500 entries written before this existed self-heal on first use.
- **live** — logged, loudly. The chunks are already with the caller by then;
  nothing can un-speak them. One bad utterance is survivable, a cached one is
  not.

A false positive costs one live synthesis — which is what an uncached line
does anyway. A false negative is a corrupted line played to a real contact on
every call. The threshold sits accordingly.

This gate is also the **wrong-sample-rate detector we never had**.
`_check_stream_rate()` was written for a stream that ignores
`speech_sample_rate`, but it can only read a RIFF header and Sarvam's
`linear16` stream is headerless — it has never fired once. 16 kHz PCM labelled
8 kHz is a 2.0× duration inflation and now lands here.

### 45.2 "bye philips lightning" — a mis-transcription offered to hang up

```
Agent  · 00:48  Got it. Which organization are you currently working with?
Caller · 00:54  bye philips lightning
Agent  · 00:55  Alright then, shall I end the call here?
```

The recording says the caller answered **"Signify, Philips Lighting"**.
Sarvam turned "Signify" into "bye", `\bbye\b` matched, and the close-confirm
gate fired mid-answer. The corpus carries the same shape at call 704 ("by the
time of interview" → "bye at the time of interview").

`bye` is a homophone of *by / buy / bhai* and STT mishears ordinary words into
it. What separates every real goodbye in 762 calls from those two is
**position, not vocabulary**: a goodbye ends the sentence.

**Fix** — `_farewell_is_terminal()` gates both `detect_intent` and
`_contains_farewell`: a goodbye word closes the call only when at most one
content word follows it. "okay thank you, bye" and "bye bye, thank you" still
close; "bye philips lightning" does not. Multi-word phrases
(`_FAREWELL_EXTRA`, per-agent `farewell_patterns`) are deliberately
unambiguous and are not subject to the test.

### 45.3 "I got a call from this number" read as a callback request

Two inbound callers returning a missed call:

```
Caller · 00:20  i got a call from this number, 15, 20 minutes before,
                regarding some engineering opening — what's the process?
Agent  · 00:23  Perfect — I'll call you back in about 20 minutes.
                Thank you, and have a great day!      [platform hangs up]
```

`_detect_callback_request()` has a weak branch: any time word plus the word
"call". A caller REPORTING a call they received satisfies both — and on an
inbound line attached to an outbound campaign, that sentence is the single
most common thing anyone says.

A second landmine in the same regex: `am` and `pm` were bare alternatives, so
`\bam\b` matched **"ma'am"**. That is how *"yes ma'am, so I just got a call
from your number"* — the caller's FIRST sentence — was answered with "which
day and what time would work best for us to call you?" (call 850).

**Fix:**
- the explicit forms (`_CALLBACK_ASK_RE`, `_BUSY_NOW_RE`) are read FIRST and
  unconditionally, so "I missed your call, ring me back at five" still works;
- `_RECEIVED_CALL_RE` (got/received/missed a call, "you called me", `call
  aaya`, `कॉल आया`, `કૉલ આવ્યો`…) withholds only the weak branch;
- `am`/`pm` now require a preceding digit.

### 45.4 The transcript showed repetition the caller never heard

Call 849, the audit transcript:

```
01:05  Agent: Understood. Could you share your current location, Mr. Piyush?
01:10  Agent: Understood. What is your current location, Mr. Piyush?
01:14  Agent: Understood, Mr. Piyush. What is your current location?
01:20  Agent: Right, that's really interesting background work. What is your current location, Mr. Piyush?
01:24  Agent: Understood. What is your current location, Mr. Piyush?
01:29  Agent: Right, so you've got a strong background in AI research and analytics. What is your current location, Mr. Piyush?
```

Transcribing the **wire recording** across the same window: the agent said
nothing at all. The caller's entire self-introduction ran uninterrupted; the
next thing they heard was a question about CTC, twenty seconds later.

The continuation merge was doing its job — it cut every one of those replies
before a frame reached FreeSWITCH — but the agent line is recorded when
GENERATION finishes, and nothing took it back. `calls.transcript` is read from
`recordings/<call_key>/transcript.txt`, so the invented repetition flowed into
the DB row, the Intelligence layer, exports and reports. **A large part of the
"repeats itself" report is this, not the agent.**

**Fix:** `_retract_unheard_agent_line()` fires at the merge cut, gated on
`_reply_still_inaudible()` read BEFORE the cut clears the playback state that
answers it. It emits `agent_retract`; the bridge calls
`audit_manager.drop_last_event()`, which truncates the transcript file back to
the entry's recorded offset — only if that entry is still the last one, and
only if speaker + text match. A reply the caller heard, or one a later write
has already followed, is never touched.

### 45.5 Asked one turn, asked again the next

```
02:05  Agent: Understood. Could you let us know how fast you could join us?
02:12  Caller: immediate, immediately
02:16  Agent: Okay, so just to confirm, could you let me know of the roles that
              you are interested in, and also how fast you could join us?
```

This one the caller really did hear. Two independent causes:

1. **The next-question directive used global slot priority**, not the current
   stage's `collect` list. So the agent asked `joining_timeline` while still
   in `RELOCATION_CHECK`, then `ROLE_AND_TIMELINE` arrived and its script —
   which legitimately owns that question — asked it again.
2. **Nothing told the model what it had just asked.** `CONVERSATION STATE`
   says "never re-ask ✓ fields", but `joining_timeline` was still ✗ because
   the extractor did not parse "immediate, immediately".

**Fix:**
- `build_prompt_context(stage_slots=…)` scopes the one-next-question directive
  to the stage's own `collect`, and names the later slots as "do NOT ask them
  yet". A stage with no `collect` (every legacy agent) keeps global priority
  byte-for-byte.
- `━━ YOU ALREADY ASKED THIS ━━` (`prompt_blocks.last_question_block`) renders
  the previous turn's question, placed **after** CURRENT GOAL so a script
  quoting the same question cannot win.
- `qa_field_signals` is widened at runtime with each slot's own QUESTION text.
  Copilot auto-fills those signals with the slot LABEL ("joining timeline") —
  a phrase no agent ever says — so `QATracker`, the second anti-repetition
  layer, matched nothing at all on any builder-built agent. `agent_copilot_api`
  now writes the question text too, for newly built ones.

### 45.6 "may merit join" → "joining in May"

```
02:59  Caller: may merit join                      # "immediate joiner"
03:01  Agent : Okay, so joining in May — got it.
```

and, in the same call, `"20 to 15 lpa"` → `"Right, so 20 to 25 LPA — got
it."`. The model repairs broken STT into a confident fact, states it back as
agreed, and it is carried for the rest of the call and into the post-call
record.

**Fix:** two lines in the builtin FIRM RULES band (both packs, so every agent
with `use_builtin_rules` gets them): never substitute the nearest sensible
value for a garbled one — ask one short confirmation or acknowledge neutrally
— and when repeating a value back, use the caller's own words and figures.

### 45.7 "Am I speaking to Piyush Gupta?" — after he just said he was

```
Caller: hello, this is piyush gupta. who is this?
Agent : This is Shubhangi from Dhron AI. Am I speaking to Piyush Gupta?
```

On an outbound call `_identity_answer()` hands the turn back to our own last
question rather than asking what the caller needs — correct in general, wrong
when the caller answered it in the same breath. Three of seven calls today.

**Fix:** the re-ask is dropped when the pending question IS the identity beat
(matched against the agent's own `identity_confirm_line` / `connect.intro_line`)
and the caller has just named or confirmed themselves. An unanswered "who is
this?" still gets the question handed back.

### 45.8 What was NOT wrong

- **The agent did not talk over the introduction.** Call 849's stage goal
  ("do NOT speak at all and only listen") was honoured on the wire; the
  merge machinery held the turn for 6+ seconds and rolled back five times.
  Only the transcript said otherwise (45.4).
- **Barge-in, VAD and STT hedging behaved.** Every repetition traced to one of
  the causes above, not to hearing.

### 45.9 Verifying it

```bash
cd calling-agent/backend && ../venv/bin/python -m pytest \
    tests/test_repetition_and_false_closes.py -q          # 61 tests

# What a poisoned cache entry sounds like (needs a Sarvam key):
#   the .pcm is raw 8kHz PCM16 — wrap it in a WAV header and POST it to
#   api.sarvam.ai/speech-to-text, then compare with the text that made it.
ls data/tts_cache/*.pcm | wc -l
```


---

## 46. A pause is not the end of a turn (2026-09-16) — ROOT-CAUSED + FIXED

**Reported:** "Whenever there is an unnatural pause in the conversation, the
agent does not respect it and keeps talking even when I am not finished…
both fragments of speech before and after should be combined as one, with
ONE response — not separate responses for every input divided by pauses."

### 46.1 What the config already did, and why it wasn't enough

Everything the good-performance flows rely on was live and correctly set:
`ENABLE_CONTINUATION_MERGE=true`, `HARD_FLUSH_SILENCE_FRAMES=20` (400ms),
`VAD_ENGINE=fsmn`, the 550ms merge window, the rollback/re-run turn
transaction, stale-fragment suppression. None of it was off.

The gap is the **idle fast path**. `TRANSCRIPT_MERGE_SKIP_WHEN_IDLE` concludes
a turn with ZERO hold whenever the caller has gone quiet and nothing is in
flight — introduced to stop an unconditional ~550ms tax on every turn, and
right for a caller answering questions. It is wrong for a caller *telling*
you something. Measured across today's shubhangi calls:

| | |
|---|---|
| engine replies | 45 |
| idle fast-paths (zero hold) | 33 |
| turns that then produced a continuation | 27 |
| … merged by the rollback layer | 17 |
| … **silently swallowed** (`Stale fragment absorbed, not answered`) | **10** |

Those 10 are the worst outcome in the whole report: the caller spoke and got
**no answer at all**, because the reply to the first half was already on the
wire. Live, from call 871:

```
05:32:46.015  [STT] 'yeah, so basically i work as an artificial
                     intelligence engineer.'          ← 10 words, complete
05:32:46.016  [ENGINE] Merge window skipped (idle)     ← zero hold
05:32:48.860  [TTS] Playing 'Got it, Mr. Piyush. What city are you
                     currently based in?'
05:32:52.732  [ENGINE] Stale fragment absorbed, not answered:
                     "so i've had experience working on ai, machine
                      learning, and "                  ← never answered
```

`_ends_with_continuation_cue` — the existing H5 guard — cannot catch this.
Every fragment is a grammatically complete sentence. **The signal is the
speaker, not the clause.**

### 46.2 The fix: read the speaker

Two signals, both cheap, both properties of who is talking
(`TURN_PAUSE_TOLERANCE`, on by default):

- **narrative** — this segment ran ≥ `TURN_NARRATIVE_SPEECH_MS` (2500ms).
  Nobody spends that long answering a question; someone mid-story pauses.
- **splitter** — this caller has ALREADY resumed through a pause on this
  call. One is a habit, not an accident. Recorded at all four merge paths
  (`_note_caller_split`): in-window merge, late drain, rollback merge, and
  the stale-fragment absorb.

When either fires:

- the idle fast path is refused;
- the merge window holds `TURN_CONTINUATION_HOLD_MS` (900ms — between the
  550ms default and the 1200ms an explicit dangling cue earns);
- the window may reassemble up to `TURN_MAX_MERGED_FRAGMENTS` (4) pieces
  instead of 1, so a three-breath answer lands as ONE turn with ONE reply.

`MERGE_HOLD_MAX_TOTAL_S` (6s) still bounds the wall clock, and the re-arm
loop only extends while caller input is demonstrably in flight.

**Short-answer flows are untouched.** A caller who never splits never trips
either signal, so Pramukh-style calls keep today's latency exactly. That is
the point of making the trigger per-speaker rather than a global window.

### 46.3 What was deliberately NOT changed

The relaxed barge-in window (`_continuation_likely_until`) stays at
`CONTINUATION_WINDOW_MS` (2s). Inside it the barge-in bar drops to ~1.3× the
VAD floor and the sustained-speech requirement halves; stretching it to 6s
for splitter callers would give the agent's own echo three times as long to
cancel its own reply. Barge-in fired on **all 9** real interruptions across
today's calls — the caller resuming is already heard. What was broken is the
agent STARTING during the pause, which 46.2 prevents.

### 46.4 Verifying it

```bash
cd calling-agent/backend && ../venv/bin/python -m pytest tests/test_pause_tolerance.py -q
# On a live call, the hold announces itself:
grep -E "TURN-TAKING|Merge window held .*(narrative|splitter)" \
     phase14_runtime/logs/docker/ws_audio_receiver.log
# And these two should trend to zero on monologue callers:
grep -c "Stale fragment absorbed" phase14_runtime/logs/docker/ws_audio_receiver.log
```

Rollback: `TURN_PAUSE_TOLERANCE=false` (whole feature) or
`TURN_SPLIT_MEMORY=false` (keep the narrative signal, drop the habit one).

### 46.5 Agent persona: Shubhangi → Aakash

Owner request, same day. `shubhangi-inbound` / `shubhangi-outbound`:

- `identity.agent_name` → **Aakash**; every spoken self-introduction now says
  **"from the Dhron AI hiring team"** (`outbound.good_time_check_line`,
  `CONFIRM_CALL_START` on both, and a new `call_flow.connect.intro_line` on
  inbound, which previously fell through to the platform-composed line).
- Voice `simran` → **`shubh`**. This is what flips the agent's own gender:
  `schema.from_dict` derives `identity.pronouns` / `personality.pronouns`
  from the TTS speaker via `speaker_gender()`, so the prompt renders "Your
  pronouns are he/him… ALWAYS use chahta, sakta, karunga" automatically and
  `address.py::_finalize_spoken` regenders any improvised first person. The
  stored `pronouns: she` was updated to `he` as well so the file is truthful,
  but the voice is the authority.
- `agents.display_name` updated to Aakash (set once at slug-claim time and
  never re-synced, so it needed a direct write).

**The slug and `agent_id` are unchanged on purpose** — `shubhangi-inbound` /
`shubhangi-outbound` carry 332 calls, revisions, campaigns and DID bindings.
Renaming them would orphan all of it. The slug is an identifier; the persona
is `identity.agent_name`.

Watch for: the old file also spelled the name **"Shubha*a*ngi"** in one
inbound stage goal, which an exact-match rename misses. Grep case-insensitively
for the stem, not the exact string.

---

## 47. Aakash gets the knowledge it was missing (2026-09-16) — SHIPPED

**Reported:** "the agent lacks a lot of content and knowledge about things it
should have."

### 47.1 What 333 calls said was missing

Mining every caller question in the shubhangi corpus (5,686 caller
utterances):

| Theme | Asks | Example, verbatim |
|---|---|---|
| Who/what is the company | ~40 | *"tell me the spelling of this company name because drone or drone or whatever"* |
| Hiring process, rounds, when do I hear back | ~20 | *"max to max how many days it will take to get a reply from you?"* |
| Role details, tech stack | ~10 | *"what tools you have?"* |
| Compensation | ~8 | *"what's the company's budget for this role, may i know?"* |
| Location / remote / relocation | ~8 | *"work in office, can i not work remotely?"* |
| Where my application came from | 4 | *"on which portal did you file my application?"* |

The agent had a one-sentence company description and one FAQ entry. On two
calls it hung up on a candidate who asked about the process (see §45.3).

**Measured TTS defect found alongside:** Bulbul pronounces "Dhron AI" as
"Drone AI" — synthesis → recognition returns `'Drone. AI'`. Four callers wrote
it back as drone/drona/droni and several asked for the spelling outright.

### 47.2 What was added

Both agents (`shubhangi-inbound` rev 13, `shubhangi-outbound` rev 10), written
through `store.write_agent` with `expected_paths` scoping and a rollback
snapshot per write:

- `knowledge_base.portfolio_prompt_text` — company, Vapi, the name-spelling
  instruction, and an explicit **WHAT YOU DO NOT KNOW** paragraph naming the
  facts the business has not supplied (rounds, assessment, callback SLA, band,
  schedule, relocation) with the instruction to defer them and never invent.
- `knowledge_base.catalog` — the four roles with `pitch_points` and `aliases`.
  **This also fixed a live bug:** the existing items used `positioning_tagline`,
  but `catalog.render_catalog_prompt` reads `positioning_tag` — the taglines
  had never rendered into a prompt.
- `response_bank.objection_playbook` — 19 entries, patterns taken **verbatim
  from the corpus** because matching is a literal lowercase substring test.
- `guardrails` — 5 never-rules and 4 always-rules (defer unknown facts, never
  imply selection, never negotiate, spell the name out, answer "can you hear
  me" in three words and continue).
- `WRAPUP_AND_CLOSE` goal — prepended a one-sentence readback of role,
  location and joining timeline; the authored sentence is preserved verbatim
  after it.
- `identity.role_description` — was a gerund phrase that rendered as *"You are
  Aakash — a warm, genuine, experienced handling preliminary screening of…"*.

Preserved and asserted in the write script: the 6 stage ids, order, `collect`
lists, `advance_when`, `role`, `turns_until_advance`; all 10 slots; both
opening beats; `languages`/voice.

### 47.3 Validating a substring playbook against the corpus

A playbook answer REPLACES the whole turn, so a false positive costs a real
screening question. Every pattern set was run against all 5,686 caller lines.
The first pass fired 95 times (1.7%) and six of those were **statements, not
questions** — the matcher has no question-shape guard:

```
"i can work from home also"                     -> remote_work
"what is the product was to integrate multimodal ai pipelines"  -> about_dhron_ai
"i am not receiving any stipend and all"        -> stipend
"please let me know if i got selected for next round"  -> hiring_process
```

Offending patterns (`work from home`, `what is the product`, `any stipend`,
`next round`, `job description`, `list them`) were replaced with interrogative
forms. Second pass: 83 fires (1.5%), all six false positives clean, all 14
real questions still answered. **Re-run this corpus check after any playbook
edit** — it is the only thing that catches a pattern that eats live turns.

### 47.4 Cost

The knowledge band is re-sent every turn: prompt grew ~4,884 → ~5,542 tokens
(+657, +13%). Breakdown: FAQ playbook 277, richer knowledge 230, catalogue
131. Partly offset — the 83 corpus turns the playbook answers deterministically
cost no LLM call at all.

### 47.5 Still deferred

Nine facts were deliberately NOT invented; the agent defers them out loud. In
priority order by how often candidates asked: interview rounds, technical
assessment, callback SLA, tech stack per role, application portal, salary
band, working schedule, relocation support, company age/size. Filling these is
per-agent content, not a code change.

### 47.6 Flow restructure (same day, owner request)

"Jumping to the questions too soon." The call now opens in four distinct
beats instead of two, and the screening questions were reordered:

| # | Stage | What it does |
|---|---|---|
| — | `connect.intro_line` | "Am I speaking to {contact_name}?" (unchanged) |
| 0 | `INTRODUCE_AND_PURPOSE` | who I am, why I'm calling, how long it takes |
| 1 | `CONFIRM_AVAILABILITY` | "Is this a good time to talk?" — gate |
| 2 | `CANDIDATE_INTRO` | "Could you start with a brief introduction?" |
| 3 | `COLLECT_SCREENING_DETAILS` | skills → location → current CTC → expected CTC |
| 4 | `RELOCATION_CHECK` | relocation willingness (unchanged) |
| 5 | `ROLE_AND_TIMELINE` | role, joining timeline (unchanged) |
| 6 | `WRAPUP_AND_CLOSE` | readback + close (unchanged) |

**The gotcha: `_maybe_advance_stage()` runs BEFORE the reply is generated**
(conversation.py, in `process_stream` right after the GREETING transition).
A new stage 0 with `advance_when: {min_turns: 1}` therefore handed the turn
to stage 1's goal and its introduction was NEVER SPOKEN — the agent went
straight from the identity confirm to "is this a good time?". `min_turns: 2`
is the shape that makes a stage speak on its first turn and hand over on its
second; the original `CONFIRM_CALL_START` used it for the same reason. Verify
any new opening stage by driving the router and printing `state.stage` per
turn, not by reading the YAML.

**Ask order is set by slot `priority`, not by the stage's `collect` order.**
Reordered to: caller_name 0, skills 1, current_location 2, current_ctc 3,
expected_ctc 4, relocation_willingness 5, role_of_interest 6,
joining_timeline 7. `current_organization` and `previous_experience` moved to
8/9 AND set `required: false` — `detect_missing_slots` only returns required
slots, so they are now captured opportunistically from the introduction and
never force a question. Their ✓ still renders when filled.

**Not re-asking what the introduction covered** is the existing machinery,
now pointed at the right moment: the background LLM slot extractor
(`_llm_extract_slots`, runs on any turn with missing slots) fills what the
introduction volunteered, `build_prompt_context` marks those ✓ with "NEVER ask
about any ✓ field", and §46's `stage_slots` scoping keeps later-stage
questions out. Verified: with skills + location + org + experience pre-filled,
the next-question directive skips straight to Current CTC. Recall depends on
the live LLM extractor — it cannot be exercised offline.

---

## 48. "Too robotic" — and what the log said was actually wrong (2026-09-16)

**Reported:** "The call is too robotic and seems AI… make it seem natural and
proactive, like a call placed by a hiring recruiter."

### 48.1 First, what the newest call proved was FIXED

Call 882 (rev 12), post-§46. Scoped to after the 07:19 deploy:

| | before | after |
|---|---|---|
| `Merge window skipped (idle)` | 33 of 45 turns | **0** |
| `Merge window held (splitter/narrative)` | — | 35 |
| `Stale fragment absorbed` (caller got no reply) | 10 | 5 |

The pause tolerance is doing its job. The new 7-stage flow ran in order.

### 48.2 The robotic voice was CONFIGURED

`personality.custom_instructions` literally said:

> "Be professional and neutral. … Use simple acknowledgements like
> 'Understood', 'Got it', or 'Okay'."

which produced, verbatim, every single turn: *"Got it, Mr. Piyush."* /
*"Understood, Mr. Piyush."* / *"Right, Mr. Piyush."* The no-flattery rule the
owner asked for earlier is compatible with warmth; "use these three
acknowledgements" is not. Rewritten to: react to what they actually said in a
short specific clause, vary the opening, use contractions, use the name
sparingly — and keep every no-flattery / no-selection-signal rule intact.
Every stiff scripted line was rewritten in speech ("One thing I should
mention — this role is fully on-site at our Vapi office…").

**`ai_disclosure` stays `deflect`.** Sounding human is a delivery choice;
*claiming* to be human is a compliance breach, and no mode may do it
(adversarial testing 2026-08-04 caught 22 denials across 8 agents). The agent
never volunteers it and never lies about it.

### 48.3 The delivery was literally chopped into pieces

The opening of call 882 went out as **four separate playback WAVs**:

```
09:53:27.776  'Thank you for confirming Mr.'
09:53:28.237  'Piyush.'
09:53:28.546  'This is Aakash from the Dhron AI hiring team.'
09:53:29.204  PLAYBACK_INVALID  duration=0.07s   ← never heard
09:53:29.205  'We received your application…'
```

`_SENTENCE_END = r'[.!?।]\s'` in the LLM stream chunker breaks after ANY full
stop — and `{title}` puts "Mr." into nearly every addressed turn, so a name is
routinely severed from its sentence. Each piece is its own TTS request and its
own WAV, separated by the bridge's ~100ms inter-file gap. **That staccato is a
large part of what "robotic" means here**, and it costs an extra TTS round
trip per fragment.

Fixed with `_ABBREV_DOT_RE` + `_next_break()`: a boundary whose full stop
belongs to an abbreviation (Mr, Mrs, Dr, a.m., Rs, e.g., month names…) is
skipped. `"We start at 10 a.m. sharp."` also stays whole.

**And a fragment is no longer silently lost.** `_validate_wav` rejects
anything under 100ms and the caller DELETED the file, so those words were
never spoken — four times in one day. `TelephonySink.flush()` now pads a
short real fragment with trailing silence to `_MIN_PLAYABLE_S` (0.12s) instead.
An empty buffer is still never written.

### 48.4 The state block was overriding the new flow

The worst finding, and a bug in §46's own scoping: `_stage_missing` treated an
empty `collect` as "no scoping" (legacy) rather than "this step asks nothing".
So `INTRODUCE_AND_PURPOSE`, `CONFIRM_AVAILABILITY` and `CANDIDATE_INTRO` were
each handed *"Ask ONLY ONE question next, about: …"*, which outranks a stage
goal. Live result:

```
09:53:44  [STAGE] → CONFIRM_AVAILABILITY
09:53:45  'Right, so what are your core technical skills, Mr. Piyush?'
09:54:28  [STAGE] → CANDIDATE_INTRO
09:54:30  'Understood, Mr. Piyush. Could you share what your current CTC is?'
```

The agent never introduced itself properly, never asked whether it was a good
time, and never asked for the introduction — i.e. **the restructure was
inert**, and the original "jumping to the questions too soon" complaint was
being re-created by CONVERSATION STATE.

`_stage_asks_questions()` fixes it: **if the flow wires `collect` ANYWHERE, an
empty list means "nothing here"**, and that stage gets an explicit *"This step
is NOT for collecting information"* line instead of a slot directive. If the
flow never wires `collect` (pramukh, pramukh_agent, demo_realty, serene_health,
nintendo_gamer — every golden-tested agent), the lists carry no information and
behaviour is byte-identical. Verified both ways.

### 48.5 The extractor invented a fact

`[STATE:LLM] Gap-filled joining_timeline='Immediate'` fired on the caller
saying *"yes, I'm available"* — meaning available to TALK. The agent later told
them they were *"available to join immediately"*; the caller corrected it to
30 days. The extractor prompt passed **bare slot names** with no meaning, so
`joining_timeline` was guessed from the name. `_slot_hint()` now appends each
slot's own question — *"joining_timeline (the caller's answer to: Could you let
us know how fast you could join us)"* — which is exactly what separates
joining from availability.

### 48.6 Also fixed, from call 880

The caller asked what other roles were open; the agent answered **and** added
*"Shall I go ahead and end the call now?"*. New never-rule: don't bolt a
hang-up offer onto an answer.

### 48.7 Still open

- **Sarvam STT credits hit 402 at 09:25** and auto-rotated. Pool key 0 is dry.
- `current_ctc` was extracted as **"44 LPA"** from "my current ctc is 4. 4 lpa
  right now" — the merge joins fragments with ". " and "4. 4" reads as 44. The
  agent corrected itself once the caller repeated. A money-slot readback would
  close it, at the cost of an extra turn.

---

## 49. One misread "no" skipped an entire screening call (2026-09-16) — ROOT-CAUSED + FIXED

**Reported:** "the agent heard what tech stack I work with and skipped towards
cutting the call… a big problem understanding what stage it is working in."

### 49.1 The two log lines that explain the whole call

```
11:00:03.927  [INTENT] "no, no, i think we can talk about it right now.
                        we can can talk something"  ->  negation
11:00:03.951  [STAGE] → WRAPUP_AND_CLOSE (branch)
```

The caller was asked *"is now a good time?"* and answered **"no, no, I think we
can talk about it right now"** — agreement. A word-level classifier sees `no`.
The `CONFIRM_AVAILABILITY` branch `{when_intent: negation, goto:
WRAPUP_AND_CLOSE}` — added in §47.6 — fired on it and jumped **five stages** to
the close, 34 seconds into the call.

Everything after that follows from it. The agent spent the rest of the call in
WRAPUP_AND_CLOSE, whose goal says to ask *"anything you wanted to ask me before
we wrap up?"* — which it asked **four times** while the candidate was still
describing his tech stack — and then hung up having collected nothing. It was
not confused about which stage it was in; it was in the wrong stage, correctly.

### 49.2 Three fixes

**A leading "no" is often a discourse marker** (`_GO_AHEAD_RE`, platform-wide).
"no no, we can talk right now", "no problem, go ahead", "no no I'm fine with
that" — extremely common in Indian English and a flat negation to a word
matcher. Checked in the affirmation block, which runs before negation. Safe
there because a genuine deferral ("call me later", "not right now", "busy") is
already returned as `objection` at the very top of `detect_intent`, above
everything. Verified against both: 6 go-ahead phrasings → affirmation, 4 real
refusals → negation, 4 real deferrals → objection.

**The branch is gone.** A branch on a bare intent at step 1 is a five-stage
trapdoor with no recovery. The platform's callback clarification already
handles a real "call me later" — it captures the time and closes — so nothing
was lost by removing it. Verified: the call-883 turn now reaches
CANDIDATE_INTRO; a genuine "no sorry, I'm driving, call me later" stays put
with `callback_requested=True`.

**A bare acknowledgement fills no slots** (`_is_ack_only`). The same call
recorded `relocation_willingness='Yes'` from the caller saying *", yeah yeah,
we can, we can."* to *"is now a good time?"* — a commitment about moving city
that nobody had asked for, which would then have suppressed the real relocation
question. A turn made only of acknowledgement/function words, with no digits,
may now fill only `_last_asked_slot`.

### 49.3 The same shape elsewhere in the fleet

```
hardik-inbound / hardik-outbound / hardik-2-inbound / hardik-2-outbound
    WARM_GREETING --negation--> POLITE_CLOSING   (skips 3 stages)
```

Not edited — they are not this owner's to restructure here — but the
`_GO_AHEAD_RE` fix protects them, since it is in `detect_intent`.

**Rule of thumb: an early-stage branch to the closing stage on a bare intent
is a bug waiting for a mis-transcription.** Prefer letting the stage goal
handle it, or gate on something with more evidence than one word.

---

## 50. Auditing an agent that has never taken a call — Bhavna Patel (2026-09-18) — AGENT FIXED + 8 PLATFORM DEFECTS FIXED

**Asked:** "analyse the agent Bhavna Patel and check how good it is; if changes
need to be done manually, do them."

"Bhavna Patel" is not a new slug. It is `jai_mistry-outbound` / `-inbound`
(owner angelo@dhronai.com) — the male real-estate qualifier "Jai Mistry",
rebuilt through the Copilot that same morning (06:59–07:20 UTC, session
`smtsbi1zv3hrp5m`) into a female (`neha`) consultative cold-caller for Craft
Idea as a *general* digital agency. DID `+91806595 3160` dials out as it. It
had taken **zero calls**, so there was no transcript to grade.

### 50.1 Method (reuse it)

1. **Owner intent** from the Copilot transcript — what they asked for, in their
   words. Every later finding is "intent vs. what the engine will do".
2. `resolve_spoken_surfaces()` for the first two beats.
3. Render `_build_system_prompt()` for every stage and **read it**. Most
   defects below are visible in the prompt and invisible in the YAML.
4. Diff against the pre-rewrite `agent_revisions` snapshot — what did the
   rewrite silently drop?
5. Drive `ConversationRouter` with lines real prospects said (mined from the
   20 earlier Craft Idea calls). **Set `LLM_TOOLS_MODE=on`** — it is set only in
   docker-compose for `lescall-runtime`; the default is `shadow`, a different
   extraction path, and the first simulation here measured the wrong one.
6. **Pace the simulation (~11 s/turn).** See 50.5 — the LLM key allows 15
   requests a minute and a turn costs 2–3.

`boot_check` and `_quality_warnings` reported **nothing** on the agent as found.

### 50.2 What the Copilot rewrite got wrong (agent-level, all fixed)

| Found | Effect on a call |
|---|---|
| Deleted 5 of 7 corpus-validated playbook answers (WHICH_COMPANY, NOT_INTERESTED, WHATSAPP_DETAILS, HOW_GOT_NUMBER, ARE_YOU_AI) and the no-invented-numbers rule | "Which company is this?" — the most common cold-call question in the corpus — was improvised again |
| Last write replaced `portfolio_prompt_text` | the owner's bucket analogy and company story vanished from the outbound agent |
| Real-estate `catalog` left in place, rendered FIRST in KNOWLEDGE BASE; brand said "15 years in real-estate" vs the owner's "10 years" | a dentist would be pitched "qualified buyer leads for builders" |
| "CCRAFT IDEA" written into spoken lines | Bulbul says **"C-Craft Idea"** — proved by a TTS→STT round trip |
| Slot `recommended_solution`, question "which solution is best for THEIR business?" | the engine would have asked the *prospect* that |
| Bare `advance_when: {max_turns: 6}` on every stage | consultation offer unreachable before turn ~12; in simulation no analogy was ever spoken |
| `scheduling` off and nothing said how to agree a time | agent invented "kal dopahar 2 baje", then claimed "maine call fix kar di hai" |
| `identity_confirm_line` / `good_time_check_line` from the old opener | prompt told the model to re-introduce itself on turn 2 |
| Inbound twin never touched | an INBOUND caller heard "Jai Mistry here… do you have a minute?" in a woman's voice |
| Literal `\n` in goals; "for Craft Idea for Craft Idea"; duplicate FIRM RULE | noise in the prompt |

Rebuilt as three history-aware phases — DISCOVERY (4 slots) →
RECOMMEND_AND_INVITE → WRAP_UP — with the owner's 9 services as the catalogue,
each carrying its analogy in English **and** Hinglish, a `WHAT YOU DO NOT KNOW`
paragraph, 12 playbook answers (46+ real caller lines matched, 0 false
positives across 8,828), and `consultation_time` as an optional `time` slot.
Applied with `store.write_agent` + scope proof: **outbound rev 30, inbound rev
19**, snapshots `agent_revisions` 288/289 (one-click rollback).

**Why three stages and not five.** The turn on which a stage's last slot is
filled is still answered under the OLD stage's goal — tool/LLM fills land after
generation — but a `max_asks` skip advances with no lag. Single-beat stages
therefore drift a beat out of step, unpredictably. Few stages, each saying "do
the FIRST step that has not happened yet", plus a `WHEN ALL FOUR ARE KNOWN`
bridge in DISCOVERY, are correct under both alignments. Shared guidance lives
in `portfolio_prompt_text` (static band, in view at every stage).

### 50.3 Platform defects this exposed (fixed, `tests/test_slot_value_hygiene.py`)

1. **A go-ahead was stored as an answer.** "हाँ जी बताओ" →
   `customer_channel='haan ji batao'`. The §49 `_is_ack_only` guard sits in the
   json-extractor branch, which production (`LLM_TOOLS_MODE=on`) never runs,
   and it knew no Hindi go-ahead verb. `_drop_non_values` now stands at all
   three doors (tool, shadow, json); yes/no are deliberately kept — "No" is
   the answer to "are you happy with your leads?".
2. **The tool roster exposed bare slot names**, so the model filled whatever
   was empty: "mostly references" → `customer_channel` *and* `business_type`.
   `build_tool_declarations(names, hints)` carries the `_slot_hint` wording the
   json extractor got on 09-16.
3. **Placeholders were values.** `consultation_time='unscheduled'` satisfied a
   `when_slot` gate and closed a call mid-invitation. `PLACEHOLDER_VALUES`.
4. **A tool-written date/time skipped the plausibility check** — "Sure, we can
   do a call" was stored as the time, and fill-only then refused "Monday 11
   am". `merge_state` now holds `source="tool"` to the same bar, in three scripts.
5. **"No time to follow up" was a decline.** The prospect named the exact pain
   the business sells against and heard "no problem, what time would be
   convenient for you?". `_strip_activity_time_complaints`: time FOR AN
   ACTIVITY is not a deferral; "बात करने का टाइम नहीं" still is. 0 of 8,828
   corpus lines change verdict.
6. **"परसों" was mis-dated twice running** (Sun 20 → "Monday the 21st"). The
   RIGHT NOW band now writes the resolved dates for kal/parso. 90 golden
   fixtures regenerated; the diff is those two lines.
7. **`book_appointment` accepted a past date** (the model wrote 2025) and the
   canned confirmation read ISO aloud. `_is_past_iso_date`, `_speakable_when`.
8. **The Copilot lint that caused half of 50.2.** It still claimed placeholders
   outside the greeting are "spoken literally" — false since §41 — and told the
   model to remove them, which is why `{honorific}` disappeared from every
   closing line. `_unresolvable_placeholder` flags only what the engine cannot
   resolve.

Also: 13 engine tests loaded the LIVE, gitignored, owner-editable jai_mistry
YAML and had been red since 07:10 that morning → frozen to
`tests/fixtures/jai_mistry_rev20/`. Two more asserted a literal `revision == 1`
against the live pramukh YAML (red since the §42 migration) and one failed
every Friday (`+1 day` = Sunday, outside calling hours). **Never run two pytest
processes at once — they share one test database and drop each other's schema.**

### 50.4 Verified

Seven simulated calls (real engine, real LLM): cooperative Hindi and English,
sceptic, decline, busy, impatient. After: clean slots, owner's analogy spoken
near-verbatim in both languages, no invented times, "parso" → Sunday 20 Sep,
decline and busy paths take the owner's scripted lines, the call closes.

### 50.5 NOT fixed — owner decisions

- **`LLM_API_KEY` is on Gemini's FREE TIER: 15 requests/minute**
  (`generate_content_free_tier_requests, limit: 15, model:
  gemini-3.5-flash-lite`). `.env` changed 2026-09-16. One call is ~13 req/min;
  two concurrent calls will 429, and the OpenAI fallback has no credits, so the
  caller hears "could you repeat that?". Found because the simulator hit it.
- **97.7% of tool turns come back without text** (335 of 343 on 09-15) and need
  the re-ask: a second LLM request and +1.22 s median on those turns.
- The dashboard **Try-agent simulator runs `shadow`**; production runs `on`.
- `agents.display_name` is written once at slug-claim and never follows a
  rename (set by hand here). `/v1` agent lists read it.
- Craft Idea facts nobody has given: working days/hours for the consultation,
  pricing, client names. The agent defers all three out loud.

---

## 51. "It talks like a robot" and "it's the same script every call" (2026-09-18) — FIXED, LIVE

**Reported** after the first two real calls on the rebuilt Bhavna Patel agent
(887, 888): *"very very slow"*, then *"very dumb… talks like a robot, I don't
feel like I am talking to a human"*, then *"it's the same script every time I
call someone — make it dynamic."* Also: *"change the voice to simran."*

### 51.1 What the calls actually showed

**Speed is a property of the VOICE, not of the pace number.** The same
135-character opening, same `pace: 1.0`:

| speaker | seconds | chars/s |
|---|---|---|
| neha | 10.6 | 12.7 |
| simran | 7.0 | 19.2 |
| shubh | 5.9 | 22.9 |

The owner had tuned 1.0 on shubh; swapping to neha made it 1.8x slower with no
setting changed (the duration gate flagged the opening at 1.34x its ceiling, so
her canned lines were never cached either). neha is also non-linear (1.25–1.35
plateau). **When a voice changes, re-measure** — synthesize one fixed line per
speaker x pace and compare seconds. Now simran @ 1.1 (6.5 s).

**Call 888, one exchange, four defects:**

```
Agent : …ज़्यादातर नए customers आपको आजकल कैसे मिलते हैं?
Caller: तो मतलब कैसे मिल रहा है मतलब?            ← "what do you mean?"
        [TOOLS] record_customer_details {'customer_channel': 'तो मतलब कैसे…?'} → 1 filled
        [LATENCY] speech_end→first_audio 5027ms
Agent : Samajh gayi, Alwyn. Waise, aapka business kis cheez ka hai?
        (10 s of silence)
Agent : माफ़ कीजिए, मुझे आपकी आवाज़ नहीं आई। Samajh gayi, Alwyn. Waise, aapka…
```

1. **A question was stored as the answer** → slot "known" → "understood", next
   question. `_drop_non_values` now also drops any value containing `?`.
2. **The re-prompt replayed the acknowledgement.** The condense-to-the-question
   rule only applied over 90 characters; a short reply was parroted whole. No
   floor now: a multi-sentence line whose last sentence asks something repeats
   only that.
3. **Five seconds of silence.** Over 518 live turns (09-15/16):
   speech_end→first_audio median **3,030 ms**, p90 4,545. On **77%** of turns
   the model answered with `record_customer_details` and no words, forcing a
   second request — median **1,223 ms**. New opt-in
   `llm_config.record_details_on_main_call: false` takes that tool off the
   spoken path (action tools stay); the background extractor captures details,
   including 1–2 word answers and an optional slot **only on the turn it was
   just asked**; a booking also fills the agent's single `time` slot. Simulated:
   second requests 0–1 per call instead of ~every turn, first words ≈1.3 s.
4. **Every line she spoke was ROMANIZED Hindi.** Sarvam's Bulbul guidance:
   *"Use native script, not romanized input — the most common integration
   mistake… significantly degrades output quality."* All authored Hindi is now
   Devanagari with English words in Latin ("आपके business में नए customers…");
   the model mirrors the script it sees, so its replies flipped too.

### 51.2 Two platform texts that manufactured the robot

- FIRM RULES said *"Open each turn with a varied natural acknowledgment (Got it
  / Sure / Right / Absolutely…)"* while NATURAL CONVERSATION STYLE forbids
  exactly that. The model obeyed the specific one: 5 of 7 replies opened
  "Right, …". Replaced in both packs (90 golden fixtures regenerated).
- My own first fix listed Hindi acknowledgements ("Achha, Ji, Samajh gayi…") —
  the §48.2 anti-pattern; it produced "Samajh gayi, Alwyn." verbatim. **Never
  hand the model a list of acknowledgements, and never put a quotable example
  in a persona — it copied "तो walk-ins भी काफ़ी होते होंगे?" word for word.**
  Describe the behaviour instead.

### 51.3 Per-call variety (new, opt-in, byte-identical for every other agent)

Same prompt + fixed opening + verbatim "suggested phrasing" = the same call
every time, even at temperature 0.7.

- `outbound.opening_variants` / `response_bank.opening_variants`
  `{lang: [line…]}` — the engine picks at random among the authored opening and
  these, once per call. A separate field because `opening_greeting` must stay
  the single string the Builder and the Copilot normalizer understand.
- `personality.style_variants` — ONE is picked per call and rendered as
  `━━ THIS CALL ━━` in the PER-CALL band (after the cached prefix).
- `router.vary_per_call = False` in previews keeps `resolve_spoken_surfaces`
  deterministic.
- An early "hello?" now gets the owner's `call_flow.connect.intro_line` when
  one exists, instead of a word-for-word replay of the opening (call 887: the
  same 10-second sentence twice was 21 of the call's 35 seconds). Only
  shubhangi-* had one and their opening is a bare "Hello", so nobody else moved.

Slot questions got a simpler `reask_question` with an everyday example — that
is what a "मतलब?" now receives. Slot LABELS are load-bearing: `_note_ask_signals`
derives "which slot was just asked" from the label's words.

### 51.4 State

`jai_mistry-outbound` rev 32, `-inbound` rev 21 (simran @1.1, 4 hi + 3 en
openers, 6 styles). `lescall-api` then `lescall-runtime` restarted with zero
live calls. Suite: 2,296 (2 failures were one test pinning the replaced rule's
wording; updated). `tests/test_call_variety.py` (22).

### 51.5 Open

- Under "आप ही बता दो" she still floats DAYS for the consultation. The cure is
  data: nobody has told the agent Craft Idea's working days.
- Prompt is ~33K chars (was 19.9K before §50) — bilingual analogies, 12 FAQ
  answers. Deliberate; ≈ +65% LLM input for this agent.
- Sarvam ranks **priya / suhani** above simran for Hindi; TTS `temperature`
  (0.6 default, 0.7–0.8 "expressive, warm") is not plumbed per agent.
- The model is `gemini-3.5-flash-lite` @ minimal reasoning for the whole fleet
  (env beats the per-agent YAML). Part of "dumb" is the model.
- The free-tier LLM key (§50.5) is still the thing that will break a campaign.

---

## 52. Re-opening a share link: REPLACE my copy, or import as NEW (2026-09-19)

**Report.** "I made an agent for another user and am trying to share it, but
when they open it, it points to the old version." The dialog said *You already
imported this agent as "jai_mistry-2-inbound". Open it from your agents list.*
and offered nothing else. Second question in the same message: *is there no
Delete? Is Archive the same thing?*

### 52.1 What had actually happened

`angelo@` built `jai_mistry` for `craftidearesearch@`, who imported it on
09-09 (`jai_mistry-2-*`). The source kept changing (27 KB → 55 KB by 09-19) and
the same link was re-sent. §26 treated a second open as an ERROR — correct for a
double-click, wrong for how sharing is really used: **agents are shared by
people who are still improving them.**

The recipient then did the only thing the product suggested — archive the old
one — and archived **one direction** (`jai_mistry-2-outbound`, 09-19 06:31).
`_previous_import` counted a copy as live while ANY slug was live, so the
inbound half kept the link locked. Nothing had told them an agent is a pair, or
that Archive is the delete. Both are now said in the confirm dialog
(`api/agents.ts::archiveConfirmCopy`, one source for both call sites).

**Archive vs delete, for the record:** there is no hard delete. `DELETE
/api/agents/{name}` moves the folder to `agents/_archived/` and stamps
`agents.archived_at`; the row stays so calls/revisions keep resolving. It frees
the plan slot only once the LAST direction is archived (the cap counts
families). There is no un-archive endpoint or UI — restoring is an operator
job (`mv` the folder back + `archived_at=NULL`).

### 52.2 The contract

`POST /api/share/{token}/import`, optional body `{mode, target}`:

| mode | meaning |
|---|---|
| *(none)* | first import. **409 `already_imported`** once a live copy exists — guessing `replace` overwrites an agent nobody pointed at; guessing `new` is the double-click bug. The 409 lists the copies. |
| `new` | another independent copy, fresh minted family, next `copy_no`. Needs a plan slot + import budget. |
| `replace` | overwrite one of MY copies with the shared agent as it is now. `target` = my family; optional only when I hold exactly one copy. Needs **neither** a slot nor import budget. |

`GET /api/share/{token}` now returns `copies[]` (my LIVE copies of this link)
and `source_updated_at`. **`can_import` answers one question only — may I take
a NEW copy?** Holding a copy no longer makes it false. That matters most at the
cap: every free account with one agent is AT its cap, so they can never take a
second copy but can always replace.

Replace is **still a one-off copy**. No link is created between the agents;
the §26 rule ("an imported agent has NO relationship to its source") is intact.

### 52.3 What replace touches, and what it must not

| | |
|---|---|
| **Overwritten** | `agent.yaml` of every direction the shared agent has — through `agent_store.write_agent`, so the recipient's version is a revision snapshot, restorable from the editor's **History** tab |
| **Kept** | the slug · **`agent_id`** · `routing.yaml` (THEIR number) · `bookings.json` (THEIR customers) · `is_active` · calls, campaigns, DID bindings (all hang off the slug) |
| **Direction I archived / never had** | **created from the shared version** and un-archived. Leaving it behind hands back half of what was shared, and the only other route is a twin *adapted from the wrong script*. The archived folder is left in `_archived/`. |
| **Direction I have, sharer no longer does** | untouched |

`agent_id` is the one that bites: `documents/store.get_folder_by_agent_id()`
returns the FIRST matching `agent.yaml` platform-wide, so writing the source's
id into the copy points this tenant's RAG at the sharer's knowledge base —
the same hazard the import remints to avoid. `_replace_spec` keeps the copy's.

Everything is validated before the first write and every write is undone if a
later one fails: a pair running two versions of one script is the state this
feature exists to get people out of.

**Documents (`_sync_documents`)** — three kinds, three answers:

* in the source → take the source's bytes
* came FROM the share, no longer in the source → **remove**. Keeping it is the
  dangerous choice: a retired price list keeps being quoted on calls.
* uploaded by the recipient → **keep**. "Replace with the shared version" is
  not consent to delete their files.

`documents/.shared.json` (a manifest of doc_ids that came from the share,
written on import and on every replace) separates the last two. Copies
imported before it existed have none, so their FIRST replace falls back to
`uploaded_at` < import time (a tree copy preserves the sharer's stamp). That
fallback is wrong after one replace — a doc the sharer added then withdrew
carries a post-import stamp and reads as the recipient's — which is exactly why
the manifest exists. Both paths are tested (`legacy_copy` parametrization).

### 52.4 Migration 0029 and three defects found on the way

`agent_share_imports`: `+copy_no`, `+synced_at`, `+sync_count`; unique index
`(share_id, importer)` → `(share_id, importer, copy_no)`. Each import claims
`max(copy_no)+1`, so two racing requests reach for the same number and one
wins. Numbers are never reused.

1. **The double-click guard did not guard double-clicks.** The unique index
   only converges two requests that hit the INSERT together. A second click
   landing after the first reservation COMMITTED found a row with `new_slugs =
   '[]'`, read it as stale, **deleted it** and imported again. A reservation
   younger than `_RESERVATION_GRACE_S` (300 s) now refuses everyone; older is a
   dead process and is ignored.
2. **Deleting the stale row reset the abuse ceiling.** `MAX_IMPORTS_PER_DAY`
   counts rows; import → archive → import deleted the row it was counting.
   Rows are kept now.
3. **A re-created slug stayed archived** (`builder._claim_slug`). The INSERT is
   `ON CONFLICT DO NOTHING`, so archive `sales` → create `sales` left
   `archived_at` set: live on disk, **uncounted by the plan cap** (a free extra
   agent) and 404 to sharing. One owner-scoped `UPDATE`, placed after the cap
   guard. No live instances (1 archived row platform-wide on 09-19).

Also: a bare pre-twin slug travelling beside a twin was named
`<family>-None` (direction came from the suffix alone) → `_variant_key`.
And the SPA's error unwrapping reads `reason`, not `message`, so the old
structured 409 surfaced as a bare "409 Conflict".

### 52.5 Frontend trap worth keeping

`bg-[var(--accent)]/10` **compiles to nothing** on Tailwind v3 — `--accent` is
a hex and v3 cannot alpha a variable it cannot parse. No warning; the class is
simply absent from the CSS. Verified by building and grepping the output. Use
`bg-mint-400/10`. `FollowupReview.tsx:292` has the same dead class (4% tint,
cosmetic, left alone).

### 52.6 Deploy — migration FIRST

```bash
set -a; . deploy/.pg_env; set +a
./calling-agent/venv/bin/python -m shared.db.migrate --status   # 0029 pending
./calling-agent/venv/bin/python -m shared.db.migrate
docker restart lescall-api
docker compose -f deploy/docker-compose.services.yml up -d --build spa
```

Safe in that order at every step: old code against the new schema inserts
`copy_no` by DEFAULT 1 and the new index still enforces one-per-importer. New
code against the OLD schema answers 503 "migration 0029 has not been applied"
rather than a 500. The `.down.sql` keeps the NEWEST copy's row per importer
(it cannot rebuild the old index otherwise) — agents themselves are untouched.

### 52.7 Tests

`test_agent_share.py` 34 → **50**. The load-bearing new ones:
`test_replace_brings_back_a_direction_the_recipient_archived` (the production
case, on the FREE plan), `…overwrites_the_copy_and_keeps_what_is_mine`
(agent_id, routing, bookings, is_active, revision snapshot),
`…can_only_ever_target_my_own_copy_of_this_link` (source / other tenant / my
unrelated agent / traversal all 400, nothing written),
`test_a_failed_replace_puts_everything_back`, the documents pair, and
`test_a_second_click_while_an_import_is_running_is_refused`.
The `_isolated` fixture now clears `entitlements` for its owners —
`_grant_headroom()` leaked, so whether a later test's TAKER was on the free
plan depended on test order.

---

## 53. Working through a filtered list: the AI filter that vanished, Next/Previous, and a filter audit (2026-09-21) — FIXED, LIVE

**Report.** "I open a call and when I come back, my special search is not active
anymore… For every call I have to search again." Plus: a control on the call
page to step to the next call in the list, and *run this filter and test that
it is up to the mark*: **"people who are willing to relocate to vapi and have
prior experience with AI under 4-7 LPA"**.

### 53.1 Why the filter vanished

The structured filters were URL state; the AI filter was **component state**
(`Intelligence.tsx`: `nlQuery`/`nlRows`/`nlVerify`… in `useState`). Opening a
row navigates to `/intelligence/:callKey`, which unmounts the list — and
`AppShell` keys `<main>` on the pathname, so it is a real unmount. Every Back
returned a fresh page, and the only way to get the rows again was another
judged pass over ~200 calls (25 s, ~200 model calls) **per call inspected**.
The breadcrumb was worse: a hardcoded `to="/intelligence"` that dropped the
structured filters too. Ticked rows died the same way.

### 53.2 The fix — what lives where

| state | home | why |
|---|---|---|
| WHICH AI filter is active | URL `?ai=` | Back, refresh and a pasted link all land on the same view |
| the judged ROWS of that filter | `lib/intelBrowse.ts` (module memory + `sessionStorage`) | a pass reads up to 300 calls with a model; re-deriving it on Back is the cost being removed |
| ticked rows | same store, keyed `ai:<q>|<agent>` / `list:<filterQs>` | a selection belongs to the filter that produced it |
| the list's row ORDER | same store (`CallSequence`) | the call page needs it for Next/Previous |

- `?ai=` present + stored rows → restored instantly, **no request**.
  `?ai=` present + nothing stored (pasted link, new tab) → the filter runs once.
- The agent dropdown changing under an active filter **re-judges for that
  scope**. Before, the rows stayed judged for the old scope while every other
  panel on the page followed the new one.
- A sidebar click to bare `/intelligence` opens the default view and OFFERS
  *Resume "…" · N calls* — it never auto-applies. An explicit *Clear AI filter*
  forgets the stored pass, so nothing is offered after it.
- A restored result older than 10 min says `as of HH:MM`. It is deliberately
  not re-run behind the user: the list they are walking would reshuffle.
- `setPage` + a second URL setter in one tick clobber each other (`lib/hooks`),
  so every multi-param change here goes through `useUrlPatch`.

### 53.3 Next / Previous (`components/CallPager.tsx`)

`‹  12 of 44  ›` at the far right of the call header (last in the row, so the
Next button holds its screen position whether or not a call has a recording),
plus **← / →**. Keys are ignored inside inputs (the transcript search; the
audio seek bar is an `<input type=range>`), with a modifier (Alt+← is the
browser's Back), and under an open dialog.

- An AI result is already complete, so the sequence is the whole result. The
  structured list is server-paged: the sequence starts as ONE page and
  **fetches the neighbouring page with the same predicates** when the user
  walks off either end. Offset paging over a live list shifts by one when a
  call arrives; `extendSequence` de-duplicates, so a shift is a no-op instead
  of the same call twice.
- Steps use `navigate(…, {replace:true})`. The user is sliding along a list,
  not drilling deeper: **one Back returns to the list** however many calls were
  stepped through.
- The list then lands on the page that holds the call last read and marks the
  row (`TR current` → `aria-current`, "· last opened").
- **"Coming back from a call" is a recorded flag (`pendingReturn`), not an
  inference.** `useNavigationType()` is `POP` for Back *and for a plain
  refresh*; the first cut used it and a refreshed list jumped to another page.
- A call reached any other way (pasted link, contact hub) gets NO pager rather
  than one that walks an unrelated list. Neighbours are prefetched — the detail
  endpoint is a pure read.

**Test:** `frontend/e2e/intel_browse.e2e.mjs` — headless Chrome against the
real production build and a synthetic `/api` that **counts `/intel/search`
hits**, because the defect is not visual: the assertion that matters is "Back
did not cost another judged pass". 17 scenarios. **17/17 on the fix, 2/17 on
the previous build** (the two neutral ones) — a test that has never failed has
not been shown to test anything. Puppeteer is borrowed from the video
workspace (`PUPPETEER_FROM`), not added to the SPA's dependencies.

### 53.4 The filter audit — method and numbers

Owner corpus 762 calls. The filter: **208 candidates, 208 examined, 0
truncated, pool not full, 24.6 s → 44 matches.** The planner read it as three
requirements (relocate to Vapi / confirms prior AI experience / salary in 4–7).

| question | how it was answered | result |
|---|---|---|
| Did retrieval miss anyone? | judged all 554 calls the search never offered, same stored checklist, retrieval removed (§32 method) | **0 misses** — not one of the 554 satisfies even requirement 1 |
| Are the 44 right? | model-free parser over the caller's answers to the agent's CTC questions, CURRENT and EXPECTED kept apart; disagreements read by hand | all 44 hold a figure inside 4–7, but see 53.6 |
| Did a gate lose anything? | verdict `checks` breakdown: 47 passed all three, 44 returned | **1 true match discarded** — 53.5 |

Per-requirement funnel (from `intel_filter_verdicts.checks`): 150 agreed to
relocate → 103 also have AI experience → 47 also pass salary.

### 53.5 DEFECT: the quote gate rejected a genuine quote — and froze the rejection

Call 616, all three checks true, confidence 1.0, dropped with *"quoted evidence
does not appear in the transcript"*. The caller said it across two VAD
segments:

```
[..] Caller: i expected
[..] Caller: 4 lakh per annum
```

The judge quoted the sentence whole. `_normalize` strips non-word characters,
so between the halves the normalized transcript still holds `caller` and the
timestamp digits — the quote can never be a substring. **28.7% of caller lines
in this corpus are continuations** of the previous caller line (§46's pausing
speakers), so this was structural, not a one-off; 4 of the 208 stored quotes
spanned segments.

Fix: `extract.speech_runs()` — each run of consecutive lines by ONE speaker,
normalized as one string; `quote_in_transcript(quote, norm, runs)` accepts a
quote found in any run. **A run never crosses a change of speaker**, so the
agent's question cannot be glued onto the caller's "yes", and two caller lines
with an agent turn between them cannot be joined either (both pinned by tests).
The commitments extractor uses the same gate, where a failed quote silently
dropped the whole commitment.

Second half of the same defect: **that rejection was cached.** A failed check
is the judge's reading of the call; a quote that is not in the transcript is a
failure of ONE model response. Cached, one bad generation excluded the call
from that filter forever. `QUOTE_NOT_FOUND` verdicts are now neither written
nor served (`load_cached` skips rows written by older builds, so they heal),
and `verified` is computed from "the model answered", not "something was
cacheable". Post-deploy: **45 matches, 207/208 from cache, 4.8 s.**

### 53.6 NOT a defect: the answer moves with the WORDING (20 / 37 / 45)

Two of the three requirements are fuzzy, and an AND of fuzzy requirements has a
wide borderline band for a lightweight judge:

- **"under 4-7 LPA" does not say WHOSE figure.** The agent asks *current* CTC
  and *expected* CTC. The planner wrote "salary, CTC, or expected package…
  falls within", and the judge flipped between them: call 820 (current 4.5,
  expected 10–11) rejected on EXPECTED; call 504 (current 6, expected ≥7 /
  ₹75k a month) admitted on CURRENT. Eight of the 44 rest on the current figure
  alone (504, 538, 604, 618, 631, 710, 765, 882). Ranges that straddle a bound
  — 6.5–7.5, 7–8 "negotiable", 6–8, 3–4 — were excluded.
- **"prior experience with AI" spans job → project → degree → interest.**
  Saying "expected CTC" fixed salary but the planner re-worded AI as
  "*discusses* prior experience" and six freshers with only an *interest* came
  in (read by hand: the original, stricter judge was right on all six). Saying
  "actually worked with AI" over-corrected to *work* experience and dropped 19
  freshers with real projects/degrees.

| wording | matches |
|---|---|
| original | 44 (45 after 53.5) |
| + "whose expected CTC is between 4 and 7 LPA" | 43 |
| + "actually worked with AI… (not just an interest)" | 20 |
| + "hands-on AI/ML experience (job, internship, projects or a degree)" | 30 |

Union 52; admitted by all four 13; by ≥3 of four **31**. Do not "fix" this in
the judge prompt — it is the requirement text doing exactly what it says. The
UI already prints the checklist ("every result had to: …"); **read it after
every filter.** The product gap, not built: rows that pass 2 of 3 are invisible.
Showing the per-requirement result per row ("Relocation ✓ · AI ✓ · Salary ✗
7–8 LPA") would turn a silent exclusion into a visible near-miss tier — the
`checks` are already stored.

Also in the results: **504, 818, 882 are the owner's own test calls**
(`918076562864`). Nothing marks a call as a test, so they sit in candidate
lists and exports.

### 53.7 Deploy notes

SPA rebuilt + `docker restart lescall-api`. Checked first that each ships ONLY
this change: files newer than the image / the container start were exactly the
ones touched here. **`lescall-workers` was NOT restarted**: it has been up since
09-12 and 23 files under `dashboard/` + `shared/` have changed beneath it —
nine days of other work it has never run, in the container that owns the
auto-dialer. The commitments half of 53.5 reaches it at its next restart; that
restart is its own decision, not a rider on a search-page fix.

A harness trap worth remembering: a bare `docker exec lescall-api python -`
has **no LLM endpoint** — `calling-agent/.env` is loaded as an import side
effect of `intel_api` (and `config` reads the environment at import). The first
ground-truth run imported `verify` directly, every judge call failed instantly,
and a polite retry loop slept through 20 minutes looking exactly like rate
limiting. Import `intel_api` first, probe once before fanning out, abort on
consecutive failures, print progress.

Tests: `test_intel_verify.py` +7 (28), intel suite + `test_sql_safety` 160
passed, `tsc` clean, e2e 17/17.

---

## 54. Prakhar pitched Surat to a caller in Vapi — one sentence, four platform defects (2026-09-24) — FIXED, LIVE

**Reported:** *"when the customer says I am from Vapi, it asks where you look
homes at, even after asking that it asks again if you want Revanta and all,
projects which are not even in Vapi … make the agent smart, good memory,
humanise the talking style."* Agent: **Prakhar** (`outbound_lead_qualifier-inbound`
/ `-outbound`, owner rajbhatu4@gmail.com). Evidence: calls 903 and 904 of the
same morning, plus outbound 473/475/481.

### 54.1 What call 904 actually did

The caller said "somewhere in Chala", "40 to 50 lakh", agreed to a visit
"tomorrow, 3 o'clock" at Pramukh Vedanta in Chala — and then heard:

> *Based on what you've shared, I think Pramukh Aranya II in Surat could be a
> genuinely good fit.*

`collected_data` for that call: `preferred_location='Chala'`,
`configuration_required='KBS'`, `finalized_property='Bhagwati'`,
`caller_name='Caller'`. The runtime log: `book_appointment rejected —
'2025-09-25' is already past` **four times**, then `[STAGE] →
POSITION_THE_RIGHT_PROJECT (advance_when met)`.

### 54.2 The four defects, none of them Prakhar's

| # | Defect | Where | Fix |
|---|---|---|---|
| 1 | **Match rules are a union, and each fires on its own fact.** `budget<=70L → Aranya II (Surat)` fired for a Vapi caller; nothing asked which city they were in. `'Chala' != 'Vapi'`, so the Vapi rule never fired at all. | `catalog.evaluate_match_rules` | `caller_city()` resolves any place-named slot to a catalogue city — directly, or through an item's own `location` ("Chala, Vapi") — and `scope_hits_to_city()` drops every candidate the catalogue places elsewhere. The prompt band names the city; when scoping empties the band it now SAYS so ("nothing there — never offer another city") instead of vanishing and leaving the whole catalogue in play. |
| 2 | **A closed slot accepted anything.** `merge_state` validated email/phone/date but stored `'KBS'`, `'Bhagwati'`, `'Chala'` verbatim from the tool; `enum_values` only ever steered the deterministic extractor. | `state_manager.merge_state` | A slot with `enum_values`/`enum_synonyms` is closed for EVERY writer: `_canonical_enum()` (exact alias → longest whole-word alias → location aliases) or **REJECTED** with an audit row. `type` decides whether the deterministic extractor runs; the vocabulary decides what may be stored — so `finalized_property` is `type: text` + the catalogue names: the LLM decides WHEN the caller has chosen, the list decides WHAT may be written. |
| 3 | **A role is not a name.** `caller_name='caller'` passed `is_placeholder_value`, the slot read filled, the agent stopped asking and said "Welcome, Baki!" to someone whose name it never had. | `llm_tools.PLACEHOLDER_VALUES` | `caller`, `customer`, `client`, `user`, `sir`, `madam`, `ma'am`… |
| 4 | **A wrong YEAR was refused as a past date.** The model passed `2025-09-25` for "tomorrow" (its training-era year) and the gate from §50 refused it — correctly by its own rule, but the caller was told the visit was locked in, nothing was stored, and the stage machine, never seeing a booking, drifted back into the recommend stage. | `conversation._execute_tool_proposals` | `_roll_forward_wrong_year()`: a past ISO date that lands within 90 days once shifted by one or two years is read as that date. `2026-09-10` on 2026-09-24 is still refused. |

And the sentence itself: the stage goal QUOTED it — *"Frame as: 'Based on
what you've shared, I think [Project] in [Location] could be a genuinely good
fit.'"* — the §51 anti-pattern; the model recited it word for word, project
filled in from the one rule that had fired.

Three more things the transcripts showed on the way:

- the objection playbook `answer`s were **instructions** ("Agree immediately
  and send it. Then note…") — a pattern hit would have SPOKEN them verbatim
  (`_match_objection_playbook` replaces the whole turn). Now `prompt_guidance`
  only, `patterns: []`;
- `objection_patterns` listed **questions** ("Is the builder reliable?", "How
  did you get my number?") as deferrals — a hit counted as a decline and got
  the canned "I'll keep it brief". Emptied; the universal deferral set stays
  (`mode: extend`);
- "end any call without confirming the WhatsApp number" made the agent ask an
  outbound contact to DICTATE their number (call 475). Now: ask only whether
  this number is on WhatsApp.

### 54.3 The rebuilt agent (rev 8 inbound / rev 13 outbound)

Written through `store.write_agent` from inside `lescall-api` (snapshots in
`agent_revisions`). Frozen copies:
`tests/fixtures/prakhar_{inbound,outbound}_2026_09_24.yaml` —
`test_recommend_before_schedule.py` and `test_prompt_now.py` read those now,
not the live YAML.

- **Flow** (6 / 7 stages, every one with `advance_when`): WARM_WELCOME →
  [INTRO_AND_TIME_CHECK, outbound] → LET_THEM_TALK (city, purpose, size) →
  SOFT_QUALIFICATION (timeline, budget — last, once, soft) → RECOMMEND_THE_FIT
  → INVITE_SITE_VISIT → CONFIRM_AND_CLOSE. `completed_when:
  {require_appointment: true}` jumps to CONFIRM_AND_CLOSE the moment a visit is
  fixed, from any stage. **Later stages RE-LIST earlier slots**: a stage that
  escapes on `max_turns` leaves its slot behind and no later stage asks it
  (the replay showed configuration stranded, and the state block calling it
  a "LATER part of the call"). A filled slot is simply satisfied, so the
  overlap costs nothing.
- **Slots** (8, typed): city (`location`, 4 cities + hi/gu/locality synonyms:
  Chala, Vesu, Godadara, Samarvarni…), purpose / configuration / timeline
  (`enum`), budget (`money`, `depends_on` city+size, `max_asks: 1`), project
  (`text` + catalogue vocabulary), callback. Questions in three languages,
  native script.
- **15 match rules, every one city-scoped**; a budget or a size on its own can
  no longer point at a city.
- **Voice**: behaviour described, never a quotable line or an acknowledgement
  list (§48.2/§51.2); 6 `style_variants`; `connect.intro_line` in three
  languages; `record_details_on_main_call: false`; `builtin_rules_pack:
  real_estate` (it was on `generic`); hours seven days 10:00–19:00 per the
  owner's brief (the Copilot had written Mon–Fri 10–18 — answer-key item
  A11); escalation to +91 87583 70000 with the 2-hour callback promise; the
  brief's 10 call outcomes.

### 54.4 Verified

Offline, real engine, no LLM: preflight `ok` for both agents in all three
languages; both opening beats resolve; replay of call 904's exact tool writes
→ `preferred_location='Vapi'`, `KBS`/`Bhagwati`/`caller` rejected, MATCH RULES
= Sahaj / Aura / Vedanta + "the caller's city is Vapi", no Aranya II outside
the knowledge section, no "genuinely good fit" anywhere; `2025-09-25 15:00` →
booked `2026-09-25`, stage → CONFIRM_AND_CLOSE. Outbound walk with a CRM
contact: WARM_WELCOME → INTRO → LET_THEM_TALK → SOFT → RECOMMEND in four turns.

`tests/test_city_memory.py` (21, inline agent — no live YAML). Full suite
2,338 passed after two live-agent fences were satisfied: the Copilot
control-surface allowlist (`controls.py`) had never learned
`guardrails.escalation.action` / `handoff_message` — keys the ESCALATION
band READS — nor the `follow_up.callback.default_delay_hours` spelling the
Copilot is told to write; and the placeholder lint refuses `{trust_proofs}`
inside `prompt_guidance` although `objection_block` resolves it, so the
proof is spelled out in the guidance instead. Two tests
re-pinned: the past-date refusal now expects the corrected year; the
office-hours arithmetic test carries its own window. `lescall-api` then
`lescall-runtime` restarted with zero live media connections.

**Not done:** no real-LLM simulated call (shared free-tier key, business
hours). The owner's next inbound call is the live check — read `[STATE]` /
`[TOOLS]` / `[STAGE]` lines for it.

## 55. Prakhar → Vikrant: one project is THE pitch (Swarit, 2026-09-24) — LIVE

**Ask (owner, patelashishk007@gmail.com):** rename Prakhar to **Vikrant**, make
him the sales agent for **Swarit** (4 & 5 BHK bungalows, New Chala, Vapi —
https://pramukh.com/swarit.html): pitch ONLY Swarit, never volunteer another
project, keep every Pramukh project as knowledge and answer any question about
any of them, redirect to Swarit, book Swarit site visits, collect name / family
size / budget / configuration / timeline / current city / visit interest /
callback time. Then, mid-work: **primary language is Hindi.**

### 55.1 What was built — platform

The match rules could not express "pitch X to everyone, keep the rest as
answers": they are a union of `when → recommend` and the caller's city outranks
them (§54), so a caller in Surat would have been offered Surat projects and a
caller who had said nothing would have been offered nothing.

`knowledge_base.primary_project: {id, mode: exclusive|lead, reason}`
(`schema.py`, parsed by `from_dict`; `controls.KNOWN_LEAF_KEYS` knows it; the
Copilot build prompt lists it):

| Where | Behaviour with a primary | Without |
|---|---|---|
| `catalog.evaluate_match_rules` | the primary is the FIRST hit before any slot is filled (`primary: True`); `exclusive` → the ONLY hit (rules stay as data for `lead`/rollback); `lead` → scoped rule hits follow it, minus itself | unchanged |
| `catalog.render_catalog_prompt` | `★ PRIMARY PROJECT — the one you pitch: …` first, with the item's DETAIL keys (`_DETAIL_KEYS`: type, tagline, address, RERA, status, amenities, layout, home_features, specifications, connectivity, landmarks, lifestyle, highlights, brochure, contact, **not_published**); the rest labelled INFORMATION ONLY (`exclusive`) or "may follow" (`lead`). `media`/`faq`/`source`/`contact_email` are data-only | byte-identical |
| `prompt_blocks.match_rules_block` | `▸ PRIMARY PROJECT: … is the project you pitch`; exclusive paragraph; a caller in another city: "The caller has said Surat; Pramukh Swarit is in Vapi. Say so plainly, ask whether Vapi could work…" — the §54 line that would call it "NOT an option" is not rendered | unchanged |
| `prompt_blocks.recommend_first_block` | "First recommend Pramukh Swarit BY NAME…" | "ONE or TWO options" |

An id the catalogue does not carry is no primary at all — a primary that
cannot be rendered must not silently demote the rest. A bad `mode` reads as
`exclusive`.

### 55.2 What was built — the agent (`outbound_lead_qualifier-{inbound,outbound}`, ids 46/47)

Slug unchanged (§46.5 — calls, revisions, campaigns and DID 7 hang off it);
`agents.display_name` → Vikrant; every spoken/authored line renamed in three
scripts (Prakhar/प्रखर/પ્રખર → Vikrant/विक्रांत/વિક્રાંત). Written through
`store.write_agent` from inside `lescall-api` by
`deploy/build_ravi_swarit.py` (rev 9/14), then a scoped write of
`languages.default: hi` (rev **10/15**). Snapshots in `agent_revisions` 310/311
(Prakhar rev 8/13 text). Frozen copies:
`tests/fixtures/vikrant_{inbound,outbound}_2026_09_24.yaml`.

- **Swarit knowledge object**: `deploy/pramukh_swarit_knowledge.yaml` — every
  value is on the page (text, captions, the layout and 3D renders, the map
  link). The site publishes **no price, no plot/carpet sizes, no possession
  date**; its seven floor-plan links are 404; the brochure only goes to a
  WhatsApp number through the site form (no public PDF). `not_published` is
  rendered into the prompt as "NOT published — never guess". RERA
  `PR/GJ/VALSAD/PARDI/Others/RAA10449/140722`; opposite Pramukh Sahaj, New
  Chala, Vapi 396191; Vapi office +91 74062 58000. The Vapi listing page also
  carries Aristo, Green County, Vivan, Silver Stone, Blue Stone — NOT added
  (not in the owner's brief, no prices); noted at the foot of the file.
- **Flow** (6 inbound / 7 outbound): WARM_WELCOME → [INTRO_AND_TIME_CHECK] →
  LET_THEM_TALK (calling_from, purpose, family_size) → SOFT_QUALIFICATION
  (family_size, configuration 4/5 BHK, timeline, budget last) →
  **PRESENT_SWARIT** (role `recommend`) → INVITE_SITE_VISIT → CONFIRM_AND_CLOSE.
- **Slots** (12): `calling_from` (text — deliberately NOT a place-named slot,
  so `caller_city()` never mistakes where they live for where they want the
  home), `family_size`, `configuration_required` (4/5 BHK first, 1–3 BHK and
  Commercial kept so an honest mismatch can be stored), `preferred_location`
  now optional + `max_asks: 0` (captured when volunteered; still scopes the
  informational catalogue), `finalized_property` vocabulary led by Pramukh
  Swarit (+ STT mishearings), `site_visit_interest` and `whatsapp_same_number`
  capture-only. Phone number is never dictated (§54).
- **Guardrails**: never pitch another project / never pretend Swarit is
  something else / never invent a price, size or possession date; always answer
  another-project questions exactly from the knowledge base, then come back to
  Swarit; "which projects do you have?" is answered honestly city by city and
  ends with Swarit as the one to see first; a mismatch (size/city) is said
  plainly, requirement taken for the team, substitute only if they ask.
  Objection playbook gained `other_project_question` and `needs_smaller_home`
  (`prompt_guidance` only, §54).
- **Primary language Hindi** (`languages.default: hi`): seeds the router's
  default via the bridge, so the second beat and the first TTS accent are
  Hindi; gu/en stay supported and evidence switching is unchanged.

### 55.3 Verified

`tests/test_primary_project.py` (30: schema + fence, candidates, knowledge
band, per-turn bands, and the frozen Vikrant snapshots — only Swarit is ever a
candidate for `{}`, Surat, and Vapi+2 BHK; preflight `ok` in en/hi/gu; second
beats name Vikrant; no Prakhar left). Regression suites green: city memory,
recommend-before-schedule, golden prompts, prompt-now, control surface,
connect-hello, language switching. `lescall-api` then `lescall-runtime`
restarted with zero live media connections; the runtime registry reports
Vikrant rev 10/15, default `hi`, `★ PRIMARY PROJECT` first in the band.

**Not done:** no real-LLM simulated call (shared free-tier key). No RAG
document was added: the embedding provider is OpenAI (`RAG_EMBED_PROVIDER`)
and that key has no credits, so a per-agent document would cost a failed
embed per turn for a BM25-only arm — the structured catalogue item carries
everything the page has. Not a migration: no env change, no DB migration
(`primary_project` lives in the YAML; `agents.display_name` is a row update).

**Rollback:** `store.rollback_agent(slug, 310|311, …)` restores Prakhar rev
8/13 text; then `UPDATE agents SET display_name='Prakhar' WHERE id IN (46,47)`.

### 55.4 First test call (908, 11:40 IST): "why did it not talk about Swarit at all?"

The caller said, in Hindi, "I was looking for a flat, in Vapi." In the
greeting stage the agent answered: *"Vapi में फ्लैट देख रहे हैं आप — Pramukh
Group के हमारे Vapi में 2 और 3 BHK के तैयार फ्लैट्स हैं, जैसे Pramukh Aura और
Pramukh Sahaj"* and asked for their name. Swarit was never said; the caller
hung up at 36 s.

Three prompt-level causes, none of them the model "ignoring" the rules:

| # | What the model was reading | Fix |
|---|---|---|
| 1 | The ★ PRIMARY PROJECT rules lived in the STATIC knowledge band ~1,200 tokens above the caller's words, and the MATCH RULES band (which carries "this is the project you pitch") is stage-gated to pitch/recommend — in WARM_WELCOME it rendered nothing. | `prompt_blocks.primary_reminder_block`: every non-recommend stage now carries a short `━━ PRIMARY PROJECT ━━` band next to CURRENT GOAL — "connect what they said to Pramukh Swarit in ONE clause (4 BHK/5 BHK, bungalows, in New Chala, Vapi)… Do NOT list or name any other project here, even one that seems a closer fit." Empty without a primary. |
| 2 | The by-location line under the knowledge band — *"A caller asking about any of them must be answered from the list above"* — read, for "a flat in Vapi", as "list what is in Vapi". | With a primary the line says the by-location answer LEADS with the primary; the other entries are named only when they ask what else there is. |
| 3 | KNOWLEDGE GROUNDING (STRICT): *"When the caller asks about anything that IS written there… ANSWER IT with those exact details FIRST"* — Aura and Sahaj are written there. | The band gains "Where Pramukh Swarit answers the question… it is the answer you give FIRST and BY NAME. The other entries are information for when the caller asks what else there is, never a list you volunteer." |

Plus the stage goals for WARM_WELCOME / LET_THEM_TALK said "no pitching yet"
without saying what to do with a requirement that is not a bungalow; they now
say: connect it to Swarit in one clause, never list other projects here even
if one sounds closer, the full pitch comes later. `calling_from` is asked as
"are you in Vapi itself, or another city?" (the tool had stored "a flat in
Vapi" as the caller's city).

**Owner's Swarit brief (pasted the same afternoon)** merged into
`deploy/pramukh_swarit_knowledge.yaml`: positioning, ideal buyer profile
(rendered as `Ideal for:` — new `_DETAIL_KEYS` entry), key selling points,
family / wellness / sports / community features, location advantages, six
customer Q&As (data-only `faq`), the seven qualification questions (one new
capture-only slot, `amenity_priorities`, asked once inside PRESENT_SWARIT).
Its two quotable lines (site-visit pitch, brochure response) are described,
not authored (§51), and "share your WhatsApp number" became "is this number on
WhatsApp" (§54). **Agents now rev 11 / 16**; fixtures re-frozen;
`test_primary_project.py` = 33; both containers restarted with zero calls.

### 55.5 Call 909: named Swarit second, dropped a question, looped at the end — and the rename to Ravi

Second test call, rev 11, 342 s, a visit booked for Saturday 26 September at
5 PM — and the owner's verdict: *"it was on a loop at the end, it made the
agent look way too dumb"*. Then: **rename to Ravi**, humanise. What the
transcript showed, and what changed:

| # | On the call | Cause | Fix |
|---|---|---|---|
| 1 | "a flat in Vapi" → *"Sahaj and Aura have ready-possession options… but if you want independent luxury living in Vapi, Swarit"* — named, but second; then "what options do you have?" → Sahaj and Aura with sizes, Swarit nowhere. | The reminder band said "don't list others"; the ALWAYS rule said "answer city by city, THEN suggest Swarit" — order was the instruction. | Band and rules now say **Swarit is the FIRST project you name, every time**, even for a flat; others only after it, in one clause, only when asked. New `what_options` guidance. |
| 2 | The caller's 6-second question ("tell me a project name… which projects are available") got no answer; 18 s later: *"Sorry, I couldn't hear you"* + the whole Swarit pitch again. | The **filler** ("one second…") that preceded the reply became the stale-fragment anchor, so a question begun 6 s earlier was absorbed as "the tail of a turn already answered"; the reply's clean completion then **wiped** the accumulator; dead-air repair replayed a 150-char single sentence because condensation needs two sentences. | `pipeline.py`: a filler never sets the anchor; a fragment absorbed while a reply plays survives that reply's completion (`_absorbed_during_reply`); `STALE_FRAGMENT_MAX_MS` (3000) — speech longer than that is a new turn, not a tail. `conversation.py`: a last line over 120 chars is not replayed — "Sorry, I couldn't hear you. Are you still there?" (`_STILL_THERE`). |
| 3 | After the booking: *"Saturday 26 September, 5 PM, confirmed"* on **five** consecutive turns; the model wrote `[END_CALL]` three times; the call ended only when the caller hung up. | Every closing turn re-proposed `book_appointment` → refused ("not legal now") → tool-only turn → text re-ask → `[END_CALL]` from the re-ask **ignored** by design (a retry must not invent a hang-up). Nothing told the model the read-back was done. | `llm_tools.build_tool_declarations(include_book=False)` once a visit is fixed or the call is closing (the tool could only be refused); the re-ask's `[END_CALL]` is **honoured when `state.is_closing`** (close-confirm still gates it); new per-turn `━━ VISIT ALREADY CONFIRMED ━━` band (`prompt_blocks.visit_confirmed_block`) from the turn after the read-back: one short human reply, never the date again, goodbye + `[END_CALL]` when they wrap up. |
| 4 | "अल्विं जी" opened or closed 11 of 14 replies; two "नमस्ते!" in the first minute. | The static guideline ("at most once every 4-5 turns") is decoration to flash-lite; the name is transliterated so no string compare sees it. | `━━ THEIR NAME ━━` band (`name_fatigue_block`): Latin name, or the `<name> जी,` / `, <name> जी।` vocative **shape**, in 2 of the last 3 agent lines → "do not say it this turn". Stage goal: "you have already said hello — never greet twice". |
| 5 | "मैं वापस से बात कर रहा हूँ" (Vapi heard as वापस), "स्वरत", "प्रमुख सेहत"; "एक महीने में खरीद लूंगा" stored nothing; `family_size` 15 not stored. | STT + vocabulary gaps. | `city_corrections` keyed on the postposition ("वापस से" → "Vapi से" — the bare word is never touched), `project_name_corrections` for स्वरत/प्रमुख सेहत, "एक महीने / one month / next month" → Immediate. `family_size` stays a text slot the LLM writes — one miss, no rule change. |

**Ravi** = the same family, rev **13 / 18**; `agents.display_name` Ravi; every
line renamed (Vikrant/विक्रांत/વિક્રાંત → Ravi/रवि/રવિ). Voice pace 1.10 on
all three voices (owner: "a little slower"; the cache key carries the pace).
Fixtures `tests/fixtures/ravi_{inbound,outbound}_2026_09_24.yaml`;
`tests/test_humanise_bands.py` (16) + `test_primary_project.py` (33);
regression suites 304 green; both containers restarted with zero calls.
`ConversationMemory.recent_assistant_texts(n)` is the helper the two
repetition guards read.

**Not fixed, worth knowing:** the second dead-air re-prompt fired while the
caller was mid-sentence — VAD missed a 24 s speech onset ("Implausible
speech span"); the reprompt logic cannot see speech the VAD did not report.

### 55.6 Owner's fact sheet: price and sizes (2026-09-24, rev 14 / 19)

The owner pasted a nine-row sheet: 46 bungalows, ready to move, 4 BHK 5,010
sq ft on a 1,960 sq ft plot, 5 BHK 5,635–6,880 sq ft on 2,100–5,000 sq ft
plots, **₹2.50 Cr to ₹3.50 Cr**, New Chala, and eight amenities (a subset of
the website's twelve). It is the only source for price and sizes — the site
publishes neither — so `deploy/pramukh_swarit_knowledge.yaml` now carries
`price_range: ₹2.50 Cr – ₹3.50 Cr` and a `specifications` map (rendered as
`Specifications: homes …; 4 BHK …; 5 BHK …; pricing …`), `not_published` is
down to floor plans and per-unit pricing, and the "pricing is on request"
lines in the stage goals, the price objection and the never-rules became
"two and a half to three and a half crore; exact unit pricing at the visit".
Owner's instruction for the delivery — *"give these details in a human
manner"* — is a `swarit_details` playbook guidance: two or three facts that
fit what THIS caller said, round spoken numbers, one question back, never
the whole list in one breath. Fixtures re-frozen; no restart needed (YAML
only; the registry re-reads on the next call).

## 56. Viraj (भारत रोजगार सेवा): the survey asked an unemployed man where he had joined (2026-09-24) — FIXED, LIVE

**Agent:** Viraj, the follow-up caller for the Bharat Rojgar Mahotsav
candidates, on behalf of MLA Dr. Rajeshwar Singh ji's office, Lucknow —
`viraj-inbound` / `viraj-outbound` (ids 86/87, owner angelo@dhronai.com),
built by the Copilot at 16:13 UTC. The number +91 80659 53160 dials OUT
through `viraj-inbound` (`phone_numbers` row 4; its inbound side is
shubhangi), so both slugs now carry the same outbound survey script.
**Ask:** *"humanise this agent … analyse the last call … change the scripts
so it's good, natural."*

### 56.1 Call 934 (17:04 UTC, 365 s, the owner playing an unemployed BSc data-science student)

| # | On the call | Cause |
|---|---|---|
| 1 | Turn 1: *"मुझे भी नौकरी ढूंढ रहा हूँ यार"* → `got_job='नहीं'` recorded by the tool. Then: which company? → what designation? → which city? → joined or pending? → **what date did you join?** → offer letter? — six questions for someone who had said he was unemployed. *"अरे मैंने बोला"*, *"अरे मैं जॉइन कैसे करूं यार … जॉइन ही नहीं किया है"*, *"मैं सेलेक्ट नहीं हुआ हूँ"*. `[STAGE] → JOB_DETAILS (turn threshold 8 reached)`. | The six stages had no `collect`, no `advance_when`, no `branches`; global slot priority walked the list in order regardless of the answer. `depends_on` cannot express it — it gates on another slot being FILLED, not on its VALUE. |
| 2 | `collected_data`: `company_name='होना है private limited'`, `designation='डेटा साइंस'`, `job_location='Delhi'`, `joining_status='अभी बाकी है'` — the office's record says a placed candidate. | The extractor fills whatever is still empty; nothing said those fields no longer applied. |
| 3 | The 20-second opening monologue (4 sentences + the question); the caller's first word at 16.4 s. | `outbound.opening_greeting` carried the whole intro; no second beat. |
| 4 | *"क्या?"* … *"समझा नहीं।"* to *"आपका पद या काम क्या है?"* | Sanskritised slot questions (पद, योग्यता, अपेक्षित वेतन, नियुक्ति पत्र, अभ्यर्थी) — the register note said "avoid formal vocabulary", the questions ignored it. |
| 5 | *"क्या इसके अलावा मैं आपकी कोई और सहायता कर सकता हूँ?"* six times; *"क्या मैं अब इस कॉल को समाप्त समझूँ?"* / *"समाप्त मान सकते हैं?"* / then the platform's *"मैं कॉल कट कर दूँ?"* — three permission asks in 90 s. | The generic ━━ ENDING THE CALL ━━ band tells every agent to ask permission "e.g. 'Is there anything else I can help you with?'" before any goodbye; this agent had no `end_conditions`, so the exception never applied. A helpdesk rule on an outbound survey. |
| 6 | *"मुझे आपके रोज़गार के बारे में बताइए"*, *"आप कैसे मदद कर सकते हो?"*, *"क्या सिस्टम रहता है?"*, *"आगे प्रोसीजर क्या है?"* — four times; answered *"हम आपकी प्रोफाइल … का मिलान करके आपको सीधे इंटरव्यू और अवसरों से जोड़ते हैं"*. | `portfolio_prompt_text: "No specific products configured"` — the model improvised a promise the never-rules forbade. |
| 7 | *"ji ji ji okay sir thank you"* to *"कॉल कट कर दूँ?"* → `close-confirm answered … — not a goodbye`; the model then replied **"You're very welcome, take care."** and the row was stamped `language=en` on a 20-turn Hindi call. | `_is_close_confirm_yes` saw seven tokens, not a bare yes. `detect_language_v3` scored the same seven Latin tokens as English with no content word among them, and the ASCII multi-word switch (≥4 words) flipped the response language; the bridge wrote `state.language` — the last flip — to `calls.language`. |
| 8 | "Alwyn जी" in 11 of 25 replies; `[TOOLS] text re-ask recovered … chars` on nearly every turn. | `record_details_on_main_call: true` → tool-only turns + a text re-ask (+1.2 s median, §54); the name band was live but flash-lite resumes after one obeyed turn. |

### 56.2 Platform — a branch VALUE can retire the questions that no longer apply

`branches[].actions: ["retire_slots:a,b,c"]` → `StateManager.retire_slots()`
(`state_manager.py`). Retired slots are a subset of `skipped`, so every
existing "leaves `missing` / counts as satisfied for `collect_satisfied`"
path holds; on top of that they are omitted from the ✗-Unknown facts list,
read *"X: not applicable."* in `running_summary` and `to_dict()["retired"]`,
and **`merge_state` refuses every later write** with an audit row — defect 2
cannot recur. `_exec_branch_action` (`conversation.py`) parses the action;
it runs before the stage moves, so the audit names the deciding stage.

Design rule: order the stages so list-adjacency after the branching stage is
the harmless half (still-looking questions do no harm to a placed caller)
and give the other half `advance_when.goto` past it (NOT_EMPLOYED →
FOLLOWUP_OFFER, else it would escape into JOB_DETAILS on its `max_turns`).

Two closing-turn classes, every agent (`conversation.py`):

- `_POLITE_TOKENS` — `_is_close_confirm_yes` strips politeness (sir, ji,
  thank you, धन्यवाद …) and accepts what is left when it is all affirmation.
  "thank you" alone still keeps the call alive; "okay thank you but one
  question" too.
- `_LANG_NEUTRAL_TOKENS` / `_language_content_words()` — the ASCII English
  switch counts content words only. "ji ji ji okay sir thank you" = 0.
- `ConversationRouter.dominant_language()` — the policy-independent majority
  language over the caller's turns (`_closing_language` uses it under
  `dominant`); `conversation_call_bridge.py` writes THAT to `calls.language`.

### 56.3 The agent (rev 3 / rev 1) — `deploy/build_viraj_rojgar.py`

- **Flow:** WARM_WELCOME (identity line) → INTRO_AND_TIME_CHECK
  (`good_time_check_line`: one breath — Viraj, the office, the Mahotsav,
  two minutes?) → CHECK_STATUS (`collect: [got_job]`, branches Yes →
  JOB_DETAILS / No → NOT_EMPLOYED, each retiring the other half's seven or
  eight slots) → NOT_EMPLOYED (`goto: FOLLOWUP_OFFER`) / JOB_DETAILS →
  FOLLOWUP_OFFER (one universal question) → FEEDBACK → CLOSING (thank, note
  taken, the office's good wishes once, `[END_CALL]`; never "anything else",
  never "may I end the call"). `end_conditions` give the survey its own
  close, which is the exception the generic band honours.
- **Opening:** "हेलो?" → *"नमस्ते! क्या मेरी बात {contact_name} जी से हो रही
  है?"* (the identity line is the second beat, verified through
  `resolve_spoken_surfaces`) → the intro line on the next turn.
- **Slots (21):** everyday Lucknow Hindi in Devanagari with the English
  words people use ("किस company में लगी है?", "आपकी पढ़ाई क्या हुई है?",
  "महीने की कितनी salary की उम्मीद है?"). Every Yes/No slot is `type:
  boolean` **plus** a closed Yes/No vocabulary with Hindi/romanised aliases
  (मिल गई, ढूंढ रहा, interview de raha…) so a tool write canonicalises to the
  value the branch compares. `job_source`, `joining_status`,
  `mahotsav_experience` are closed enums; `current_status` is capture-only;
  `max_asks: 1` on everything a caller may not want to answer.
- **Knowledge band** (`ABOUT BHARAT ROJGAR SEVA`): what the office does —
  notes status and needs, follows up with a company when asked, shares
  matching openings when they come — and what it does NOT (guarantee a job,
  a company, a salary, a date; place anyone directly). Playbook guidance
  (never quotable answers) for "what do you do", "what happens now", "how
  did you get my number", a complaint, "I already told you", busy, not
  interested. **These lines are the engineer's reading of the brief — the
  office should confirm them.**
- `objection_patterns: []` (the Copilot list had "don't want" — "I don't want
  to relocate" would have counted as a decline); `record_details_on_main_call:
  false`; pace 1.10; 5 `style_variants`; `closing_language_policy: dominant`.

### 56.4 Verified

Offline, real engine, no LLM (`--dry-run`, then `--apply` inside
`lescall-api`): preflight `ok` both languages; both beats resolve; 'नहीं' /
'अभी ढूंढ रहा हूँ' / 'interview de raha hoon' → `No` → NOT_EMPLOYED with the
seven placed slots retired, a later `company_name='होना है private limited'`
refused, summary "not applicable"; 'हाँ' / 'मिल गई' → `Yes` → JOB_DETAILS
with the eight seeker slots retired; NOT_EMPLOYED fully answered →
FOLLOWUP_OFFER. `tests/test_viraj_rojgar_flow.py` (32) + the closing/state
suites (161 green); fixtures `tests/fixtures/viraj_{inbound,outbound}_2026_09_24.yaml`.
Both containers restarted 18:08 UTC with zero live media connections.

**Not done:** no real-LLM simulated call (shared free-tier key). The owner's
next dial is the live check — read `[STAGE] → NOT_EMPLOYED (branch)`,
`[BRANCH-ACTION] retire_slots`, `[STATE] retired …`, and that the last
`[LANG] Response language:` line stays `hi`. Also seen on 934 and not
addressed: three double replies (00:50/00:52, 03:02/03:06, 04:31/04:34) where
a continuation cut the first reply mid-playback and both lines reached the
transcript — the §45.4 retraction covers a line never heard, not one half
heard.

---

## 57. Document upload: both stages died with the OpenAI key; moved to Gemini, and the upload now tells the truth (2026-09-25) — FIXED, LIVE

### What was reported

"Check whether documents can be uploaded properly, what API is being used,
whether there are credits remaining — and choose the best one for our use
case: clients give us documents about their business for the agent build."

### What was actually happening

Three surfaces take a document — the Agent Builder's *Documents · RAG* tab
(`POST /api/agents/{slug}/documents`), the Create wizard
(`POST /api/agent-tools/extract-document`) and the Copilot paperclip
(`POST /api/copilot/upload` → the `ingest_document` tool). All three funnel
into two stages in `calling-agent/backend/app/documents/`, and **both stages
ran on `OPENAI_API_KEY`**:

| stage | module | model | key |
|---|---|---|---|
| field extraction (document → agent YAML fields) | `yaml_filler.py` | `gpt-4o-mini`, hardcoded `openai.OpenAI(OPENAI_API_KEY)` | OpenAI |
| vector index for in-call RAG | `embeddings.py` | `text-embedding-3-small` | OpenAI |
| OCR for scanned PDFs / images | `extractor.py` | `gemini-3.1-flash-lite` | Gemini |

Probed live on 2026-09-25, every configured key:

| key | state |
|---|---|
| `OPENAI_API_KEY` | **429 `credit_balance_exhausted`** on embeddings AND chat (dead since 2026-09-11, §43) |
| `ANTHROPIC_API_KEY` | 400 "credit balance is too low" |
| `LLM_API_KEY` / `GEMINI_API_KEY` (two different keys, same project) | funded, paid tier (18 parallel flash-lite calls → 18×200), `cachedContents` works; embedding models on both: `gemini-embedding-001`, `gemini-embedding-2`, `gemini-embedding-2-preview` |
| `SARVAM_API_KEY` | funded (STT/TTS); has a Document AI parser (Sarvam Vision, 10 pages/file, 10 rpm, async job) — no embeddings |

So: on **2026-09-24** a client's brief attached in the Copilot
(`ClientScriptLesCallReq.docx`) failed **four times in one day** with
`[docs/yaml_filler] Extraction error: Error code: 429 - You have no credits
remaining`. And every document uploaded in the Builder since 09-11 had
"embedded" nothing — the upload returned `status: uploaded`, the tab toasted
*"Uploaded & embedded"*, and the document was keyword-searchable only. The
API did report it (`GET /documents` → `index.needs_reindex`, built in §28)
but the tab never rendered that field.

Four more defects found on the way, none of them about credits:

1. **The Builder upload parsed the file ON the event loop** — `extract_text`
   (pdfplumber, and the Gemini OCR fallback of up to 120 s) and
   `extract_fields_from_text` (up to 12 LLM calls) were called inline in the
   async handlers. That is the exact shape of the 2026-07-20 incident where one
   Copilot ingest 502'd every endpoint of the API. Only `/reindex` used
   `asyncio.to_thread`.
2. **No size cap** on the Builder endpoint (the Copilot's had 15 MB): the whole
   body was read into memory unconditionally. nginx caps the request at 25m.
3. **The pickers and the server disagreed.** The Copilot's `accept` offered
   `.doc`, which python-docx cannot open (→ "Text extraction failed:
   Package not found"); the Builder's refused `.md`/`.csv`, which the server
   read fine; nobody accepted `.xlsx`, which is what a price list arrives as.
4. **`Document.paragraphs` skips every table** — a client's objection/answer
   grid or price table in a `.docx` extracted as nothing.

And one that predates all of this: the redesigned Documents tab's Upload
button was `<Button>` inside a `<form onSubmit>`, and the design-system
`Button` always renders `type="button"`. The click submitted nothing. Thirty
days of `lescall-api` logs hold exactly one document upload, and it came
through the Copilot.

### Which API, and why

**Gemini, on the platform's own funded key, for both stages.** Not because it
is the only funded key (it is), but because it is the right one for this use
case:

* **Extraction** (`yaml_filler.resolve_llm()`): the platform LLM endpoint
  (`LLM_BASE_URL` / `LLM_API_KEY`, Gemini's OpenAI-compatible route), model =
  `COPILOT_LLM_MODEL` (`gemini-3.6-flash` today). Reading a client's brief IS
  agent building, so it shares the Copilot's quota bucket, never the live-call
  model's. `DOC_EXTRACT_MODEL` / `_BASE_URL` / `_API_KEY` override each part;
  `OPENAI_API_KEY` remains the last resort. JSON mode
  (`response_format: json_object`, probed OK on the compat route) with a
  one-time fallback if an endpoint rejects the parameter;
  `reasoning_effort: low` and `max_tokens: 16000` because Gemini's thinking
  tokens count against `max_tokens` on that route (the intelligence extractor's
  truncated-JSON trap). Chunks grew 12k×4 → **60k×6 chars**: the old cap
  silently dropped everything past 48k chars, and the result now says
  `truncated` when the new one bites. The three themed passes stay — they were
  what recovered recall on unlabeled documents (Kaapd, 2026-07-15).
* **Embeddings** (`embeddings.GeminiEmbeddingProvider`): `gemini-embedding-2`
  (GA; $0.20 / 1M tokens, on the pricing page — `gemini-embedding-001` no
  longer is), **native `batchEmbedContents`**, not the OpenAI-compat route the
  intelligence module uses, for three things that route does not carry:
  `usageMetadata.promptTokenCount` (compat returns `usage: null`, so the ledger
  would log 0 tokens forever), **task types** (`RETRIEVAL_DOCUMENT` for
  chunks, `RETRIEVAL_QUERY` for the caller's turn — the model is trained on
  that asymmetry), and row ordering (compat returns `index: null`). 768 of
  3,072 Matryoshka dims; the model returns unit vectors for truncated dims
  (probed: norms 1.0000). Batches of ≤100 (101 → 400 INVALID_ARGUMENT).
  Ingestion now has its own 60 s deadline — the old provider applied the 2 s
  QUERY cap to ingestion too, so a long document could time out at upload.
* **Sarvam Document AI** was evaluated and not adopted for parsing: a
  10-page-per-file ceiling and an async job you poll, versus a text layer we
  already read for free and a Gemini OCR fallback that takes the raw PDF
  inline.

### Measured before switching (the §28 doctrine)

Golden set, 37 questions over the 10-document corpus, `rag_eval.runner
--live --embed-model gemini-embedding-2` (vectors now in the frozen cache):

| | text-embedding-3-small (§28) | gemini-embedding-2 @ floor 0.6 | **gemini-embedding-2 @ floor 0.85 (shipped)** |
|---|---|---|---|
| Recall@1 | 0.79 | 0.79 | 0.79 |
| Recall@3 | 0.98 | **1.00** | **1.00** |
| MRR | 0.893 | 0.905 | 0.905 |
| NDCG@5 | 0.902 | 0.913 | 0.913 |
| Precision@3 | 0.53 | 0.43 | 0.52 |
| Cross-doc contamination | 0.054 | 0.214 | 0.125 |
| Over-abstention | 0.00 | 0.00 | 0.00 |
| Citation accuracy | 1.00 | 1.00 | 1.00 |
| Query embed latency (live) | p50 320 / p95 622 ms | p50 573 / max 860 ms (10 sequential) | same |
| Cost per query | ₹0.000014 | ₹0.00024 (12.9 tokens) | same |

Recall and ranking improved; contamination doubled at the OpenAI-tuned
relative floor, because Gemini's cosine scores sit in a narrow band (the
absolute floor never fires up to 0.55 on this set) and `0.6 × best` pruned
nothing. Re-swept offline (table in `config.py`): **`RAG_RELATIVE_FLOOR`
defaults to 0.85 for the Gemini provider** — the last point where nothing else
moves; 0.90 starts paying Recall@3. The residual 0.125 is the same
`wrong_context` pair as before (two documents about the same company).
`rag_eval/baselines/current.json` is now the Gemini baseline; the OpenAI one
is kept as `openai_text_embedding_3_small.json`, and `--embed-model
text-embedding-3-small` still runs offline against it.

The latency is real and on the voice path of any agent with documents: about
+250 ms at p50 per retrieving turn versus OpenAI. Inside the 2 s cap
(`RAG_EMBED_TIMEOUT_S`); past it the turn proceeds on the lexical arm.

### What shipped

* `app/documents/config.py` — provider-aware defaults: `RAG_EMBED_PROVIDER=gemini`,
  `gemini-embedding-2`, dim 768, key `LLM_API_KEY` (falls back to
  `GEMINI_API_KEY`), `RAG_EMBED_BATCH_SIZE`, `RAG_EMBED_INGEST_TIMEOUT_S`,
  `RAG_RELATIVE_FLOOR` 0.85/0.6. `RAG_EMBED_PROVIDER=openai` reproduces §28.
* `app/documents/embeddings.py` — `GeminiEmbeddingProvider`, `build_provider()`,
  `embed_queries()` (query task type + short deadline; `embed_query` prefers it
  when present), and **`embed_error.json`**: a failed embed writes the
  provider's sentence next to the chunks, `index_status` reports it as the
  row's `detail`, the next successful embed removes it. `runtime.available`
  tells the UI when there are no credentials at all.
* `app/documents/extractor.py` — `SUPPORTED` / `ACCEPT` / `unsupported_reason()`
  are the one list every picker reads; `.xlsx` (openpyxl, every sheet, values
  not formulas), `.docx` walks paragraphs AND tables in body order, plain-text
  BOM handling, and each refusal names the way out (".doc → save as .docx or
  PDF", ".pptx → export as PDF", a renamed binary → "upload it with its real
  extension").
* `app/documents/store.py` — `sha256` + `size_bytes` on every document;
  `find_document_by_hash()`.
* `app/documents/yaml_filler.py` — `resolve_llm()`, JSON mode, larger chunks,
  `_friendly_error()` (quota / key / model / unreachable → one sentence each),
  result carries `model`, `chunks`, `truncated`.
* `dashboard/agent_builder_api.py` — one gate for every surface
  (`_read_document_upload`: 415 with the way out, 400 empty, 413 with the
  limit — `DOC_UPLOAD_MAX_MB`, default 20); parsing and extraction in
  `asyncio.to_thread`; the upload reply carries `embedded`, `index_state` and
  `index`; a byte-identical re-upload answers `status: duplicate` with the
  original; `extract-yaml` errors are a 502 with the extractor's sentence
  (they were a 200 carrying `{"status": "error"}`, which opened the review
  dialog on nothing).
* `shared/ai_pricing.py` — `gemini-embedding-2: $0.20/1M`, sourced.
* `rag_eval/runner.py` — `--live` builds the production provider for the
  model (Gemini or OpenAI); default model `gemini-embedding-2`.
* Frontend — `api/agents.ts` (`DOC_ACCEPT`, `DOC_MAX_MB`, `docSupported`,
  `DocsList`/`DocIndex`/`UploadDocResult`, `reindexDocs`); the Documents tab
  rebuilt: drop zone, the one accept list, client-side type/size refusal,
  a click-driven upload (no `<form>`), an **index-state chip per document**
  (Indexed / Needs re-index / Keyword search only, with the reason), a
  **Re-index** banner when anything is out of the index or when no
  credentials are configured, and an upload toast that says what happened
  ("Uploaded & indexed — 5 chunks" / "keyword search only: HTTP 429 …" /
  "Already uploaded as …"). Create wizard and Copilot paperclip share the
  list and the size check; `.doc` is no longer offered.
* Tests — `tests/test_documents_upload.py` (22): the type/size gate, xlsx
  and docx-table readers, the Gemini provider against a mock transport
  (batching, task types, dims, tokens, error sentence, client reset), the
  embed-error round trip, dedupe, the builder replies, `resolve_llm`, JSON
  mode, chunk merge, parameter fallback and the quota sentence. Note the
  fixture: the dashboard imports `backend.app.documents.*` and the tests
  `app.documents.*` — two module objects — so it patches BOTH `_AGENTS_DIR`s.
  The first draft patched one and wrote a document into the real
  `calling-agent/agents/` and embedded it against the real endpoint.

### Deployed

`docker restart lescall-api` and `lescall-runtime` (no call in progress),
SPA rebuilt. The two agents with documents (`dhron_calling`, `kaapdfabrics`)
were `stale` ("index built with 'text-embedding-3-small'") and were re-indexed
in place: both `ok` on `gemini-embedding-2`, 768d, 5 chunks each.

Verified end to end on the client document that failed on 09-24: extraction
now runs (gemini-3.6-flash, 1 chunk, 4.3 s) and returns **zero fields — which
is correct**: that file is our own blank intake checklist ("Company name, the
name as it should be spoken…") with no business facts in it. Every Gemini
model tried (3.6-flash, 3.5-flash, 3.5-flash-lite, with and without JSON
mode) returned `{}`. The Copilot tells the user nothing was found and lists
what is missing; it no longer says "429".

### Not done / for whoever is next

* `app/web/server.py` (the CLI `--web` console) still has copies of the
  document endpoints that parse inline; it is not on the product path.
* The `ingest_document` tool still merges into the interview DRAFT (the
  agent-creation path). For an existing agent the user must ask the Copilot
  to apply the extracted fields (§47).
* Gemini's `embedContent` for `gemini-embedding-001` returns no usage; only
  `gemini-embedding-2` does. Do not switch back to 001 expecting the ledger
  to fill.
* `PPTX` is refused with "export as PDF"; `python-pptx` is not in the image.

---

## 58. Twenty "declined" calls that never rang: a spreadsheet phone column stored as numbers (2026-09-25) — FIXED, LIVE

### Reported

"The group created from angelo@dhronai.com has numbers incorrectly stored —
no +91 — I expected it to be filled automatically. The calls were dialled and
show rejected/declined. Was a real call even placed?"

### What happened

Group 19 ("dhronai hiring") was filled from `Dhronai_ Candidate Profiles __
NxtWave.xlsx` (20 rows, 06:32 UTC) and campaign `986674df` dialled all 20 at
06:34–06:36. Stored numbers: `+91286928650`, `+83097276130`, `+74114944320`…
— every one an 11-digit string that is a valid Indian mobile **plus a trailing
0**. The country code WAS applied automatically; it was applied to the wrong
digits. `to_e164` saw 11 digits not starting with 0 and, correctly for a real
international number, kept them as `+83097276130` (no such country) or
`+74114944320` (a Russian number).

**A real originate was placed for each**, through the Plivo gateway, and the
carrier answered inside a second — `freeswitch.log.1` (the live file; logrotate
left `freeswitch.log` at 0 bytes since 2 Aug):

```
06:34:15 Originate Resulted in Error Cause: 1  [UNALLOCATED_NUMBER]   x15
06:34:34 Originate Resulted in Error Cause: 21 [CALL_REJECTED]        x5
```

No handset rang anywhere. `_parse_fs_result` folded both causes into
"declined" — the same word a human pressing reject produces — which is the
third time this confusion has cost a day (§40, and craftidea's four dials
on 09-09).

### Root cause

`_parse_uploaded_file`'s xlsx branch did `str(cell)`. openpyxl returns a
numeric phone cell as a **float** when the file stored it that way (this
export did), so `str(9128692865.0)` is `"9128692865.0"`, and
`_normalize_phone_cell` — "strip everything that is not a digit" — produced
`91286928650`. The scientific-notation guard (`9.12869E+09`) existed; the
plain float form did not. A CSV exported by pandas ("9128692865.0") and a CRM
pushing the same string through the public API had the identical hole,
because every path ends in `shared.phone.digits_only`.

### Fixed

* **`shared/phone.py` — `numeric_text()`**, applied inside `digits_only` (so
  `to_e164`, `significant_digits`, `dialable_destination` and every write
  path share it): a fraction after seven or more digits is formatting noise
  and is dropped; scientific notation is expanded with `int(float())`.
  `"9128692865.0"` → `+919128692865`; `"9.128692865E9"` → the same;
  `"+18001021111"`, `"079-23251306"`, `"23250771/72"` untouched.
* **`dashboard/server.py` — `_xlsx_cell_text()`**: an integral float cell
  prints as an integer, so the artefact never appears in the first place.
* **`invalid_number` is a first-class call status.** `_parse_fs_result` maps
  `UNALLOCATED_NUMBER` / `INVALID_NUMBER_FORMAT` / `NO_ROUTE_DESTINATION` /
  `NO_ROUTE_TRANSIT_NET` to it, ahead of the "declined" bucket, which keeps
  `CALL_REJECTED` (a person or their network turned the call away). It is in
  `CALL_STATUSES`, both campaign tallies, the phone-book filter and stat
  tiles, the Activity feed filter and the campaign badge ("Invalid number").
  An invalid number does NOT auto-set disposition `rejected` — it is a data
  problem, not a lead decision.
* **Every dial now logs the carrier's words** next to the bucket:
  `CAMPAIGN [id] 83097276130 -> invalid_number (-ERR UNALLOCATED_NUMBER)` and
  `DIAL … → invalid_number (…)`. "Was a call even placed?" is answered by
  `docker logs lescall-api`, not by reading the FreeSWITCH log as root.
* **Data repaired**: the 20 rows of group 19 rewritten to `+91` + the first
  ten digits (checked: 0 collisions with existing contacts), `call_status`
  back to `not_called`, `disposition` `not_classified`, `call_count` 0,
  `last_called_at` NULL, call history cleared — because nothing about those
  people was learned. **Not re-dialled**: placing 20 real calls is the
  owner's decision. The other 18 eleven-digit rows of this account are
  genuine `+1 800…` US toll-free numbers and one `+31…`/`+968…` and were left
  alone; `patelashishk007`'s `+91926586400` is a nine-digit typo, not this bug.
* Tests: `tests/test_phone_numeric_cells.py` (the normaliser on nine
  spellings, an xlsx with float / int / text cells through the real parser,
  a CSV with both artefact forms, the cause → status mapping).

### How to tell next time

"Declined" in under 2 s, for every row of a list, with `call_count` 1 and no
`calls` row (a dial that never connects writes none — §33) is a number
problem. Read the phone column: a Python float leaves a `.0`, Excel leaves an
`E+09`, and both leave 11 digits where an Indian mobile has 10.

## 59. GCS writes were refused for three days and nothing said so: the probe was a LIST (2026-09-26) — FIXED, LIVE

**What happened.** On 2026-09-16 05:25 UTC the billing account behind project
`lescall` went delinquent. From then on every GCS **write** from this VM
returned `403 The billing account for the owning project is disabled in state
delinquent` — recordings, exports and shipped logs alike — while every
**read** still worked. `lescall-workers` logged `sync complete: uploaded=0
… failed=19` and `WARN recsync pass failed` every 15 minutes for three days.
The sentinel's hourly "GCS round-trip" passed every hour, because it was a
one-object `list_blobs`, and reads were fine. Nobody was told. The owner
found it by accident on 2026-09-19 and settled the account within the hour:
the 06:05 UTC pass read `uploaded=17 failed=1`, the next `failed=0`, and by
2026-09-26 every one of 927 calls is stamped `gcs_uploaded_at` and
`shipped_logs` is current. No data was lost — retention re-confirms the
bucket object before it deletes a local copy.

**Why the class is bigger than one incident.** The platform's contract with
the buckets is WRITE. A probe that reads answers a different question, and
the two answers diverge exactly in the failure modes that matter: billing
holds, a bucket made read-only, an object-level role narrowed to viewer. And
the one place the failure WAS visible — a WARN line inside the workers log —
is not somewhere anyone looks.

**Fix (shared/sentinel.py):**

* `check_gcs` now WRITES: one ~40-byte object per bucket
  (`GCS_PROBE_BUCKETS` = recordings + logs), then deletes it. Both verbs are
  in the attached SA's object-level role (`storage.objects.create/delete`),
  which the write also proves. A failed delete after a landed write is a
  warning, not an outage.
* `gcs_failure_kind()` reads the error: `billing account` in the message →
  alert key **`gcs-billing`**, whose body says settle the account in Cloud
  Console and that nothing on the VM can fix it; a 403/401/credential
  message → the existing **`gcs-auth`** body (ADC file, SA role, scopes),
  which now also points at `deploy/provision_vm_sa.sh`. A billing hold sent
  to the auth runbook costs a day in the wrong console.
* Recovery pops both alert keys so the NEXT incident emails at once instead
  of waiting out the 6 h repeat window (the `voice_health` rule).
* New `check_recsync` (every 5 min, from the same `docker logs
  lescall-workers` text `check_workers` reads): `recsync_stuck()` pages
  (**`recsync-stuck`**) when the last `SENTINEL_RECSYNC_STUCK_PASSES` (2)
  recording-sync passes each read `uploaded=0 failed>0`. One such pass is a
  WAV still being written; two (30 min) is a stall. A pass that landed
  anything is progress even with failures — the 09-19 recovery pass
  (`uploaded=17 failed=1`) must not page.
* Tests: `tests/test_sentinel_gcs.py` (13) — the classifier on the real
  delinquency message (which IS a 403 and must not read as auth), a
  permission-denied, a `RefreshError`, a timeout; the stall rule on two
  failing passes, one, progress-with-failures, recovery, tail-only, too
  little history, the pre-08-07 line format.

**Can the VM fix this itself? Measured, no — and it does not need a new
service account.** The attached SA is `lescall-recsync@lescall.iam.
gserviceaccount.com` (scope `cloud-platform`) and holds exactly
`storage.objects.{create,get,list,delete}` on both buckets — enough to do
its job, which is the only thing a service account could contribute here. It
cannot read the project's billing state, cannot create or bind a service
account (Cloud Resource Manager and Cloud Billing APIs are disabled on the
project; the SA has no IAM rights), and both human logins in the host's
`gcloud` are expired. A billing hold is settled by a person with a card, not
by IAM.

`deploy/provision_vm_sa.sh` codifies the check and the repair for the day
either is needed (a new VM, a rotated SA, a narrowed bucket role):

```
deploy/provision_vm_sa.sh              # as the VM: attached SA, scopes, a real write per bucket
deploy/provision_vm_sa.sh --provision  # needs `gcloud auth login` by an OWNER first:
                                       # enables the IAM APIs, creates lescall-recsync if absent,
                                       # binds roles/storage.objectAdmin on both buckets, prints
                                       # the stop/set-service-account/start steps if the VM runs
                                       # as a different SA (never runs them — that is the phone
                                       # lines down), re-runs the check
```

It creates no SA key (org policy blocks it; the attached-SA design needs
none — §6b) and its check output names billing vs IAM the same way the
sentinel does.

**Also this day:** `lescall-workers` had been up since 2026-09-12 with 14
files changed under it (the §53 quote-gate `extract.py`, §58 `phone.py`,
`recording_storage.py`, `verify.py`…). The tick-loop passes are fresh
processes every tick and had been picking those up, but the two long-lived
processes — `shared.intelligence.worker` (which runs the follow-up sweep)
and `shared.billing.sync` — were 14 days stale. Restarted 04:48 UTC with the
queue idle (1084/1084 done, no pending scheduled call, 0 live calls); back
in 3 s.

**Open, unrelated, seen in the sentinel log:** `inbound-silence:8031339140`
has been firing since 2026-08-05 — DID +91 80 3133 9140 has taken no inbound
call in 1,242 h. Either the number is unused and the alert should be retired
for it, or it is a dead line.

### How to tell next time

`grep -c 'ALERT\[gcs-' phase14_runtime/logs/sentinel.log` and
`docker logs --since 2h lescall-workers | grep 'sync complete'`: the first
names billing or auth within an hour of the first refused write; the second
shows `uploaded>0` or `failed=0` when the buckets are healthy. `deploy/
provision_vm_sa.sh` answers "can this VM write its buckets right now?" in
two seconds without a login.

## 60. One agent, two "outbound" rows: a twin's direction is its SLUG, and a lint crash after the write said "DID NOT HAPPEN" (2026-09-26) — FIXED, LIVE

**What happened.** The Agents card for Viraj (भारत रोजगार सेवा) showed two
`outbound` rows — one "Activate", one "Live on +91 80659 53160" — and no
inbound row. The card draws one row per family member and labels it from the
YAML's `call_direction`; `viraj-inbound/agent.yaml` said `outbound`, and had
since 2026-09-24 16:14 UTC (`agent_revisions` 322 → 323). It was the only
mismatched family in the fleet.

**How it got there** (Copilot session `smufpz2seq2mnzu`, 16:01–16:14 UTC):

1. The owner pasted the Hindi survey script. Gemini answered with
   `update_draft` whose `call_flow.scheduling` was the JSON **text**
   `'{ "enabled": false }'`, not an object. `_normalize_definition` guards
   top-level sections against wrong shapes but not nested fields;
   `from_dict` reads `{}` for a non-dict; and `_quality_warnings`
   (`agent_copilot_api.py`, the `_sched.get("style")` line) crashed on the
   string. That lint runs AFTER the draft is merged, so the draft took the
   change while the tool result said `FAILED … This operation DID NOT
   HAPPEN`. Eight turns in a row — the poisoned draft broke every later
   preview too — with the model reporting "I ran out of steps before I
   could change anything".
2. "make the agent" → `create_agent` wrote BOTH twins (`[copilot] created
   agent pair 'viraj'`, 16:13:01) — then the same lint crashed and the model
   was told "DID NOT HAPPEN".
3. It retried `create_agent` and got "Agent 'viraj-inbound' already exists —
   use update_agent to edit it" (the existence loop names the inbound twin
   first; the primary was actually `viraj-outbound`, matching the draft).
   It obeyed: `get_agent viraj-inbound`, `update_agent viraj-inbound` with
   the whole outbound definition, `call_direction: outbound` included
   (rev 323). "Still to do: matching the outbound version" — out of steps.
4. Nothing anywhere tied a twin's `call_direction` to its slug: not the
   store, not the Builder PUT (the Edit page has a Call Type selector), not
   the Copilot's `_invariant_violations`, not `_mirror_to_twin` (which
   blocks the field on the mirror only). The §56 rebuild script then wrote
   `outbound` onto both slugs again, deliberately.
5. The tool loop's `except Exception` produced the FAILED envelope with NO
   log line — the API log for those minutes carries no error at all; the
   only record was the session file. A second session that morning
   (`smuf94n8oz44z8n`, a document build) hit the same crash with
   `scheduling: 'flexible'`.

**Why it matters beyond the card.** `list_agents` derived the counterpart
from the data: `viraj-inbound` (direction outbound) → counterpart
`viraj-inbound` → `has_counterpart` true, so the family could never offer
"Add inbound version" and `create_twin` would 409 against itself. And
`conversation.py` reads `_def.call_direction` for the identity-confirm
line and the opening: bound to the inbound slot, this YAML would have asked
an inbound caller "am I speaking with …" — the "outbound agent behaves like
a receptionist" class `_warn_direction_mismatch` only logs.

**Fixes (platform-wide).**

- `store.slug_direction` / `twin_of_slug` / `assert_direction_matches_slug`
  / `DirectionLocked`: `write_agent` refuses a `call_direction` that
  contradicts a `-inbound`/`-outbound` suffix, BEFORE the snapshot (no stray
  revision row), and fills a MISSING field from the slug. One gate for the
  Copilot update, rollback, Builder PUT and share import;
  `write_agent_dir` applies it at creation (422). Standalone slugs are
  unconstrained. The message names the twin to edit instead.
- `list_agents` derives `counterpart` from the slug (`counterpart_of`);
  data only for standalone slugs.
- Builder Edit page: Call Type is locked for a twin, with the reason
  (`CallDirectionSection locked`), so nobody fills the form and meets a 422.
- Copilot: `_decode_stringified_objects` in `_normalize_definition` (JSON
  text → object for every dict-typed nested field; scheduling words
  `flexible`/`false`/`off`… mapped; any other non-dict in a STRUCTURAL
  field dropped with a note; per-language spoken fields untouched);
  `_quality_warnings` tolerates any scheduling/calendar shape;
  `_safe_quality` wraps all five lint call sites so an advisory can never
  turn a landed write into FAILED; the tool loop logs `logger.exception`
  for anything but a ValueError; create's "already exists" names the family
  and the twin matching the intended direction; the system prompt tells the
  model a twin's call type is fixed by its name.
- `deploy/build_viraj_rojgar.py` sets each twin's direction from its slug.
- Data: `viraj-inbound` restored to `call_direction: inbound` (rev 4, actor
  `direction-repair`), content unchanged, still unbound. Number 4 binds
  inbound → `shubhangi-inbound`, outbound → `viraj-outbound` (unchanged).
- Tests: `tests/test_direction_identity.py` (15): the algebra, the store
  refusal (no file change, no revision row), the fill, standalone freedom,
  rollback refusal, Builder PUT 422, single create under a twin name, the
  listing's counterpart under corruption, the decoder, the word map, the
  spoken-field exemption, lint survival, the wrapper, and the exact
  `update_draft` payload of the incident recording `draft_updated`.

### How to tell next time

```bash
for y in calling-agent/agents/*/agent.yaml; do s=$(basename $(dirname $y)); \
  c=$(grep -m1 '^call_direction:' $y | awk '{print $2}'); \
  case $s in *-inbound) [ "$c" = inbound ] || echo "MISMATCH $s $c";; \
             *-outbound) [ "$c" = outbound ] || echo "MISMATCH $s $c";; esac; done
docker logs lescall-api | grep 'tool .* crashed'         # now carries the traceback
```

A Copilot tool result of `FAILED` with `'<type>' object has no attribute
'get'` is a shape crash; a revision reason `copilot update: call_direction,
…` on a suffixed slug was the signature of this class — the store refuses it
now, and the tool result names the twin to edit.

**Pre-existing, unrelated, seen while running the suites:**
`test_copilot_control_surface.py::test_every_live_agent_key_is_known_or_provably_dead`
fails on the live `dechcept_inbound-inbound` / `-outbound` agents built on
2026-09-25 — they carry YAML keys the control surface does not know. It
scans the live agents dir (the class the "engine tests never load a live
agent YAML" rule exists for) and was red before this change.

## 61. Call 971: the caller answered every question and the record kept almost none of it (2026-09-26) — FIXED, LIVE

Viraj (`viraj-outbound`, भारत रोजगार सेवा survey caller, RUNBOOK §56) placed
its second test call at 05:24 UTC to Alwyn (call 971, conn `33efbec3`). Four
minutes, Hindi, cooperative caller, every question eventually answered. What
the caller heard, in order, and what the record said afterwards:

| What happened on the line | Where it came from |
|---|---|
| "हेलो, नमस्ते।" **three times** before the agent said who it was | `outbound.opening_variants` picked "हेलो, नमस्ते।"; the early-hello repair replays the last line unless it is a *bare hello*, and two greeting words are not a bare hello (`_is_bare_hello_text`) |
| The identity stage (WARM_WELCOME) never ran — the intro arrived on the first real turn | the caller's "जी नमस्ते" is an `affirmation`; `advance_when {intent: affirmation}` advanced stage 0 before it had spoken (third sighting: 791, 934, 971) |
| `mahotsav_experience = 'Okay'` at 00:35, from "हाँ ठीक रहेगा" (consent to a two-minute chat) | `_extract_typed` matched every enum alias anywhere in the call; "ठीक" is in the Okay vocabulary |
| "It was good, it was good… a good experience" → agent: "हाँ, अनुभव ठीक-ठाक रहा" | the slot was already 'Okay' (closed vocab + fill-only), the model read the state back |
| "…Lucknow से बाहर भी जाने के लिए तैयार हैं?" to a man who had just said he lives in Delhi → "लखनऊ कहाँ से आ गया?" | the slot's authored question named the *office's* city; the goal said "outside Lucknow" |
| `relocate_outside_lucknow = 'No'` from "बट मैं रहता तो दिल्ली में हूँ, लखनऊ कहाँ से आ गया?" | the extractor's `answer` path stored a yes/no from a QUESTION |
| `company_contacted = null` after "नहीं, अभी तक नहीं आई"; `future_assistance = null` after "रोजगार की मदद चाहूंगा"; `issues_faced` never asked | `note_asked` moved every `max_asks: 1` slot to `skipped` the moment the directive CHOSE it; the extractor reads only `missing`, so the answer one turn later had no key to land in. `issues_faced` was burned by a directive the model did not follow (it asked the stage goal's first beat instead) |
| "माफ़ कीजिए, आपकी आवाज़ स्पष्ट नहीं आई" after the caller said "हाँ हाँ हाँ बिल्कुल" | `validate_transcript` rejected it as `repetition` (3/4 = 0.75); a following 860 ms segment with 300 ms of voice returned '' and the empty-streak damper spoke on the second empty — in the platform's sarkari wording, not the agent's own `lang_fallbacks` |
| A 3.6 s Hindi answer arrived 5.4 s late | a script-rejected segment ahead of it went to Whisper, which has **no credit** (429 `insufficient_quota`) — attempt + retry ≈ 4 s, then dropped; in-order consumption held the good transcript behind it |
| "ठीक है, तो फिर मैं कॉल कट कर दूँ?" | `termination.confirm_before_end: true` (the platform default) asked the exact question the script forbids in three places |
| "note कर लिया" five times | `register_notes.hi` listed it as a phrase to say (§51: a quotable line is said verbatim) |

### Engine fixes (every agent)

- **A greeting-only line carries nothing worth replaying** —
  `ConversationRouter._is_greeting_only_text` (hello ∪ namaste/namaskar/good
  morning… ∪ filler) is now the AGENT-side test everywhere `_is_bare_hello_text`
  was used for "is there anything to replay?": the early-hello replay, the
  connect fast path, the onset intro, the dead-air reprompt, `_last_question_asked`,
  `connect_intro_line`. Caller side, `_is_early_greeting` triggers the fast path
  on "जी नमस्ते" too: a greeting answering a greeting IS contact, and the
  identity beat is due. `_is_early_hello` (replay of a REAL opening) is
  unchanged. The Devanagari danda is no longer glued to the token.
- **An outbound call's first stage speaks before an intent can end it** —
  `_maybe_advance_stage` holds an `intent` advance on stage 0 while
  `discovery_turns == 0` and the connect intro has not been delivered. Inbound
  agents are untouched (their opening already does stage 0's job).
- **An everyday word is a slot value only as the answer to its own question** —
  `state_manager._EVERYDAY_ALIASES` (ok/fine/good/ठीक/अच्छा/सही/हाँ/नहीं…):
  `_extract_typed` stores such an alias only when `_is_being_asked(slot)` — the
  engine's reading of its own last line (`observe_user(asked=…)`, from
  `_last_asked_slot`), the directive's choice on the previous turn, or the head
  of `missing`. A city, a project or "linkedin" is still taken wherever the
  caller volunteers it. Also: the LONGEST alias at a position wins ("not bad" is
  not "bad"), and edges are script-aware (`_word_boundary_ok`) — `isalnum()` is
  False for every matra, so "मिल" matched inside "मिली". Booleans read their
  vocabulary first and use the same edges (`_BOOL_YES_RE`/`_BOOL_NO_RE`; the old
  `\b` form never matched "हाँ" or "नहीं" at all).
- **The ask budget settles on the caller's turn, not on the choice** —
  `note_asked` at max_asks now records `_ask_exhausted[slot]`; the slot leaves
  `missing` (never re-asked) and counts as satisfied for the stage gate exactly
  as before, but `detect_missing_slots()["awaiting"]` lists it and
  `_spawn_slot_extractor` reads `missing + awaiting`, so the answer one turn
  later has a key. It becomes `skipped` on the caller's second turn if still
  empty (`_settle_asks`). `unnote_asked` + `_reconcile_ask_budget` give the
  budget back when the agent's spoken line demonstrably asked a DIFFERENT slot
  (`_last_asked_slot` or `QATracker.last_asked_field`).
- **A caller's question never writes a yes/no** —
  `_drop_yes_no_from_question_turn` at both extractor doors (json + tool):
  when the utterance asks something and does not LEAD with a yes/no, boolean
  and enum values are dropped; free text keeps what the question carries.
- **STT**: a repeated affirmation beside another word is collapsed, not
  rejected (`_REPEAT_OK_WORDS`; pure "na na na" stays noise); `marathi_*`
  rejections get the same forced-language retry as Gurmukhi misfires
  (`_SCRIPT_RETRY_REASONS`); a Whisper quota error opens a 15-minute breaker
  (`fallback_available()`, `STT_FALLBACK_QUOTA_COOLDOWN_S`) — no attempt, no
  retry, no hedge. The pipeline's "couldn't hear you" line is the agent's own
  `response_bank.lang_fallbacks[lang]` when it wrote one, and the empty-speech
  gate reads `AudioSegment.speech_ms` (VAD real speech) instead of the buffered
  span.

### Viraj (deploy/build_viraj_rojgar.py → viraj-outbound rev 2, viraj-inbound rev 5)

Relocation question names no city ("किसी और शहर में मिले, तो वहाँ जाने को तैयार
हैं?"; the goal says anchor on the city THEY named); feedback question is open
(the three levels are for the record, never a menu; "good" is good even with a
"but nothing came of it"); the register note describes acknowledgement variety
instead of quoting "note कर लिया"; `confirm_before_end: false`; `still_searching`
optional and out of NOT_EMPLOYED's collect; Devanagari-English rating synonyms
(गुड/ओके/नॉट बैड/बैड). Frozen snapshot: `tests/fixtures/viraj_outbound_2026_09_26.yaml`.

Tests: `tests/test_call971_viraj.py` (49). `test_stage_machine_v2::test_max_asks_skip`
now asserts the awaiting → skipped sequence.

### How to tell next time

```bash
grep -E "REPAIR\] Early 'hello'|CONNECT\] identity_confirm_line|STAGE\] .*has not spoken yet" phase14_runtime/logs/docker/ws_audio_receiver.log | tail
grep -E "everyday word .* ignored|last ask; skipped if unanswered|ask budget restored|caller asked a question" phase14_runtime/logs/docker/ws_audio_receiver.log | tail
grep -E "repetition collapsed|NO QUOTA|fallback provider out of quota" phase14_runtime/logs/docker/ws_audio_receiver.log | tail
```

A `[REPAIR] Early 'hello' #1 — replaying last response` whose response is two
greeting words is this bug; `source=regex` on an enum slot on the turn the
caller said yes to something else is this bug; a `null` for a slot the
transcript plainly answers, with `hit max_asks … skipped` one turn earlier, is
this bug. **Whisper still has no credit** — the breaker hides the latency, not
the outage; top up or remove the fallback.

## 62. Agent-scoped LLM context cache: the agent band is billed once per agent, not once per turn (2026-09-26) — LIVE

**Why.** Viraj call 972 sent ~7,950 input tokens per turn, 20 turns, `cached=0`
every time, although the static band's fingerprint never changed. Billing is
now on (`[PROMPT-CACHE] … AVAILABLE`), but Gemini's implicit cache never
engages on the OpenAI-compat route. So nothing was cached.

**What.** `app/voice/context_cache.py` creates one explicit `cachedContents`
object per AGENT (namespace = slug from the bridge, else `agent_id`; key =
namespace × model × sha256(agent band + tool roster)) and serves every turn
of every call of that agent from it.

| Band (`_prompt_bands`) | Where it goes | Shared? |
|---|---|---|
| AGENT: language lock, identity, mission, brand, portfolio, grounding, guidelines, personality, style, anti-repetition, objections, firm rules, closure | cache `systemInstruction` (+ the tool roster) | every call of this agent, this language |
| PER-CALL: direction/identity lines, customer context, THIS CALL style | first user message (`CALL_HEADER`) | never |
| PER-TURN: CALL STATE, slots, goal, RAG, reply trailer | last user message (`CONTEXT_HEADER`) | never |

Measured provider constraints (2026-09-26): a request naming `cached_content`
may not carry a system instruction, tools or tool_config (400). Tools
therefore live in the cache (one cache per roster: with/without the booking
tool). The compat usage block never reports cached tokens, so the ledger is
fed the size returned at cache creation (`_turn_cache_tokens`).

**Placement was measured, not guessed** (Viraj, 10–12 samples per arm):
everything-first ended the call with a tool-only `end_call` in 5/20 FEEDBACK
turns (uncached 0/20); everything-last made the agent re-ask "क्या मेरी बात …
हो रही है?" mid-survey. Split (call first, turn last) matched uncached on all
three stages tested.

**Safety.**
- Creation, TTL refresh (`LLM_CONTEXT_CACHE_TTL_S`=1800, refreshed below 900 s
  left) and deletion of a superseded revision's cache are background,
  single-flight tasks. A turn uses a cache only if it is READY.
- Any request refused because of the cache kills the entry and the SAME turn
  is re-sent uncached (`_primary_stream_create`). The fallback provider never
  sees the cache. `force_text` re-asks go uncached.
- Pre-warm at `set_call_context` builds the agent band ONLY — a full prompt
  build would run the slot directive and spend an ask budget at turn 0.
- Registry persists to `data/llm_context_cache.json` (gitignored) so a runtime
  restart reuses live caches.
- Golden prompts are byte-identical (`full == agent + "\n" + call + "\n" + turn`).

**Knobs.** `LLM_CONTEXT_CACHE=false` (kill switch),
`LLM_CONTEXT_CACHE_DISABLED_AGENTS=slug1,slug2`, `LLM_CONTEXT_CACHE_TTL_S`,
`LLM_CONTEXT_CACHE_MIN_TOKENS` (1024), `LLM_CONTEXT_CACHE_NEGATIVE_TTL_S`.

**Not cached, on purpose.** Spoken replies (85.5% unique, a replay is a
correctness hazard); the slot extractor and classifiers (~700-token prompts,
below any provider floor); per-call/per-turn text (caller data must never be
shared). TTS audio already has its own disk cache.

**How to tell.**
```bash
grep -E "CTX-CACHE" phase14_runtime/logs/docker/ws_audio_receiver.log | tail
grep "LLM-USAGE" phase14_runtime/logs/docker/ws_audio_receiver.log | tail   # cached= should be ~4.8k on Viraj
```
`created … (N tokens, hi:…)` once per agent/language/roster; `stats hits=…`
every 25 hits; `invalidated` + `re-sent uncached` means the provider dropped
a cache and the turn was saved. Tests: `tests/test_context_cache.py` (23).

## 63. Call screeners, voicemail and the mid-call "hello": a machine is not the customer, and a hello is not an answer (2026-09-28)

**Report (Craft Idea).** "When the person is an iPhone user, voicemail often
gets activated at the beginning of the call … the client was saying hello
hello, but the AI agent keeps speaking the script." Call
`919426600929_20260928_094925` (`jai_mistry-2-outbound` rev 6, Nisha):

| Heard | Engine did (before) | Why |
|---|---|---|
| iOS Call Screening: "if you record your name and reason for calling, I'll see if this person is available" | good-enough intro from the LLM, BUT stored the sentence as `customer_channel`'s answer, pinned STT to en-IN, advanced GREETING→DISCOVERY | nothing knew what a screener is |
| screener: "thanks" / "please (stay on the line)" | "You're welcome!" | gratitude shortcut fired on any turn containing "thanks" |
| the person picks up: "hello" | "Right, so how do most new customers find you…" — a person who never heard who was calling | QA band said `customer_channel` was ANSWERED ("…reason for calling"); outside the 25 s hello window a bare hello went to the model |
| "hello" | "Right, so what kind of business do you run?" — the NEXT question | same; "Every turn must … ask the next missing field" |
| "okay okay" | "Of course!" + next question; `business_type` marked answered with "okay okay" | QA tracker counted any turn as an answer |

Corpus sweep (1,076 calls): 33 calls on 7 agents had a machine on the line —
20 voicemail (agent said "I'll call you back later" to a mailbox, or pitched
it), 9 screeners, 4 hold announcements (agent said "I'll wait right here" to a
recording four times in one call). Plus 3 calls where the model spoke
Perso-Arabic words mid-Gujarati ("word of mouth થી જ زیادہ customers").

**What (platform, every agent).**
- `app/voice/machine_line.py` classifies a caller line as SCREENER / HOLD /
  VOICEMAIL / UNREACHABLE. Anchored on machine phrasing only; validated on all
  10,511 caller lines — zero hits on people ("no, not available right now,
  call me 10 minutes after" stays a person). `tests/test_machine_line.py`.
- `ConversationRouter._machine_line_gate` runs FIRST in `_process_stream_raw`.
  Machine turns never reach memory, slots, QA, language or the stage.
  - SCREENER → one deterministic identity line in the screener's language
    ("Hi, this is Nisha from Craft Idea, calling for {contact_name} regarding
    your business. I'll stay on the line."), then silent wait. Override per
    agent: `call_flow.connect.screening_line: {en: …}`.
  - HOLD → silence. Screener courtesy fragments ("thanks", "please") → silence.
  - The first PERSON after a screener: language restored to what the call had
    before the machine spoke; a bare hello gets `connect_intro_line()` (the
    owner's `intro_line`, else the opening) — they heard nothing yet.
  - VOICEMAIL / UNREACHABLE → hard close in silence (`call_outcome`,
    `extra_flags.machine_answered`). Optional
    `call_flow.connect.voicemail: {action: leave_message, message: {hi: …}}`
    waits for "at the tone / record your message" (or the mailbox going quiet)
    and speaks it once.
  - Voicemail/screener wording from someone ALREADY in conversation is a
    person ("leave a message after the beep on my other number").
- Pipeline: a machine line is never language evidence (mirror + pin skipped);
  the dead-air reprompt and silence hangup stand down while a screener/hold
  has the line (`awaiting_machine_handoff`, ceiling `MACHINE_WAIT_MAX_S`=60).
- Bridge: `answer_state='empty'` with reason `voicemail_detected` /
  `unreachable_announcement` / `screened_no_pickup` / `hold_no_pickup`.
- **Mid-call hello = presence check** (`_presence_check_reply`): 1st bare
  hello → "जी, आपकी आवाज़ आ रही है।" + the last QUESTION sentence
  (`_last_question_sentence`, keeps "Mr. Rajiv" whole); 2nd in a row → the
  composed identity + "can you hear me clearly?"; then the model. Inside the
  25 s window, once the second beat has been spoken, the same rule replaces
  the verbatim 10-second replay.
- QA tracker: a question-only turn answers nothing; a bare ack ("okay okay")
  answers only a boolean slot. The QUESTIONS ALREADY ANSWERED band was what
  told the model to move on.
- Gratitude shortcut only on a gratitude-ONLY turn ("thank you, 2BHK works"
  used to get "You're welcome!" and lose the 2BHK).
- Prompt: the style block quoted "Right, so a 2BHK…" / "Right, so actually—";
  12.5% of English agent lines opened with "Right". Examples removed, behaviour
  described. Guidelines now say a non-answer is answered and the SAME question
  re-asked. Golden prompts regenerated (diff = these lines only).
- English approval filler no longer "Of course!/Absolutely!" to a caller's
  "okay".
- `_finalize_spoken` drops Perso-Arabic script before TTS (no voice reads it).

**Verify.** `grep -E "\[MACHINE\]|\[REPAIR\] (Mid-call|Second hello)" phase14_runtime/logs/docker/ws_audio_receiver.log`

## 64. WhatsApp follow-ups: a caller who says yes on the call gets the brochure / form (2026-09-29) — BUILT, sending OFF

**Why.** About 14 agents promised "I'll send the details on WhatsApp" on live calls
and nothing ever sent them. The promise was recorded three different ways (the
`promise_whatsapp_media` branch flag, a `whatsapp_number` slot, a
`commitments.kind='whatsapp'` row) and nothing read any of them.

**What.** Integrations → WhatsApp (`/integrations/whatsapp`, sidebar group below
Intelligence). A tenant connects their OWN WhatsApp Business number, gets a
template approved by Meta, and writes a **playbook**: agents → template →
attachment / tracked link → placeholder mapping → **Manual review or Automatic**.

```
call ends → intel pipeline … notify → whatsapp stage (shared/whatsapp/stage.py)
  consent.detect(call)            flag | whatsapp slot yes/number | commitment whose
                                  quote is IN the transcript; a "no"/DND beats all
  → wa_consents row (evidence = the caller's words)
  → one wa_sends row per (playbook, call)   UNIQUE — re-runs never duplicate
       auto → queued      manual → pending_approval (+ bell notification)
       any field empty → pending_approval even under auto
lescall-api sender loop (shared/whatsapp/sender.py, every 20s)
  re-checks: kill switch, connected, canary, newest consent, send window (IST),
  daily cap, template APPROVED → Meta Graph API → wamid
Meta webhook /api/whatsapp/webhook → receipts (forward-only), replies, STOP, template reviews
```

**Guards that matter.**
- Calls older than an hour never send (`FRESH_WINDOW_S`): the sweeper backfills the
  new stage over every historical call, and without this it would message months
  of past callers at once. Backfill observed clean (0 errors, 0 sends).
- Consent is its own append-only table, NOT `contacts.disposition` — `rejected`
  also means "declined one ring". Newest row per phone decides; STOP = revoked.
- `{honorific}`/`{title}` come only from `contacts.gender` (never guessed, §42), and
  Meta refuses an empty template variable — so use `{greeting}` ("Mr. Rakesh Shah"
  / "Rakesh Shah" / "Sir") and give every variable a fallback. A send with an
  empty field waits for a person.
- Access tokens are Fernet-encrypted (`WA_TOKEN_KEY`). **Back that key up** —
  changing or losing it means every connected tenant must reconnect.
- `/r/<token>` tracked links are open (a customer taps them, no session) and only
  redirect to the one URL the token names. nginx has `location /r/` and an exact
  `location = /api/whatsapp/webhook` (rate limit + 1 MB cap).

**Config** (`calling-agent/.env`, read at call time; restart `lescall-api` after editing):
`WA_SEND_ENABLED` (kill switch, **false**), `WA_CANARY_PHONES`, `WA_TOKEN_KEY` (set),
`WA_VERIFY_TOKEN` (set), `WA_APP_SECRET` + `WA_APP_ID` (**from the Meta app — not set
yet; the webhook answers 503 until then**), `WA_API_VERSION`, `WA_PUBLIC_BASE_URL`.

**To go live.** Meta business verification → Meta app with WhatsApp product →
paste `WA_APP_SECRET`/`WA_APP_ID` into `.env`, set the webhook URL + verify token in
the app, subscribe `messages` + `message_template_status_update` → tenant connects
in Setup (WABA id, phone number id, system-user token) → template approved →
playbook → `WA_CANARY_PHONES=<own numbers>` + `WA_SEND_ENABLED=true` → one real
call → then remove the canary.

**Not done yet.** Agents still promise WhatsApp whether or not a playbook exists
(the engine should say "our team will share it" when none is enabled); the Copilot
prompt still says the platform cannot send WhatsApp (change it when sending goes
on); the call page / ContactHub do not yet show the send chip or consent (API:
`/api/integrations/whatsapp/calls/{call_key}`, `/contacts/{id}/consent`);
Embedded Signup; replying from the dashboard; the automation `send_whatsapp`
action is still a stub. `deploy/PLAN-whatsapp-followup.md` (July) is superseded.

**Files.** `shared/whatsapp/*`, `dashboard/whatsapp_api.py`, migration `0030_whatsapp`,
`frontend/src/pages/integrations/*`, `frontend/src/api/whatsapp.ts`.
**Tests.** `tests/test_whatsapp.py` (59): placeholders (gender tables asserted equal
to `address.py`), templates, consent, signature/STOP, stage, sender re-checks,
webhook, tenant scoping, `/r/` redirect.

## 65. Ravi: "a long pause after I confirm who I am" — the yes was thrown away (2026-09-30) — FIXED, LIVE

**Report.** Ravi (`outbound_lead_qualifier-*`, and angelo@'s copy `-2-*`) sometimes
went silent after the caller confirmed their identity. Across the 28 Ravi calls that
asked "क्या मेरी बात X जी से हो रही है?", 5 dropped the answer (994, 997, 998, 1006,
1008): 4–8 s of silence, then "माफ़ कीजिए, मुझे आपकी आवाज़ नहीं आई…" or the caller
had to say hello again. Every answered confirmation still took p50 2.9 s / p90 3.8 s.

**Cause 1 — the answer was absorbed as the pickup hello.** The identity line is spoken
on the ONSET of the caller's pickup "hello" (§ connect onset), and for 8 s
(`CONNECT_ONSET_ABSORB_S`) any greeting-only turn was absorbed as that hello's late
transcript. `_HELLO_FILLER` counts हां/हाँ/जी, so once the pipeline glued the pickup
hello onto the answer — continuation merge of the provisional "हेलो।" (1008), the
stale-fragment accumulator prepending "नमस्कार" (998), an in-window merge (994) — the
merged "हेलो। हां जी हां जी।" was greeting-only and was dropped.

**Cause 2 — the good-time beat went through the model.** `outbound.good_time_check_line`
was only quoted in the prompt, so every "yes" paid a 1.1–2.3 s LLM turn to regenerate
(and reword) a line the owner had already written, and a "हाँ, एक सेकंड." filler when
it was slow.

**Cause 3 — the pickup armed the splitter hold.** The same glue counted as "caller
resumed through a pause", adding `TURN_CONTINUATION_HOLD_MS` (900 ms) to every later
turn (20 of 46 calls).

**Fixes (platform, every agent).**
1. The pipeline records when the onset intro finished PLAYING (`_onset_play_end_ts`,
   from the sink's `playback_busy_until`). Speech that ENDS after it
   (`ONSET_REACTION_GRACE_S`, 0.2 s) is the caller's answer: `end_connect_onset_absorb()`
   closes the absorb window. End, not start — a merged transcript keeps the hello's onset.
2. A pickup greeting spoken over the onset intro is dropped in the pipeline (never a
   turn, never accumulator context). An absorbed hello in the router is rolled back
   (`rollback_pending_turn`) so it cannot be merged into the next turn.
3. `_note_caller_split` ignores splits before the first real turn inside the connect
   window (`_in_pickup_phase`).
4. `_good_time_fastpath_line`: when the agent's last line contains the identity
   question (onset intro, dead-air re-ask or presence re-ask — matched on the line's
   longest sentence, any language variant) and the caller's turn is a plain yes
   (`_is_identity_yes`: every token a yes / go-ahead / "मैं ही बोल रहा हूँ" word, at
   least one real yes; "हेलो जी" is not one), the authored `good_time_check_line` is
   spoken at once, no LLM. Once per call. Opt-out: `call_flow.connect.good_time_fastpath:
   false`; kill switch `GOOD_TIME_FASTPATH=false`.
5. `_presence_check_reply` no longer treats "hello जी हां" as a line check when the
   agent's last line was a question — हां/yes after a question is its answer.

**Tests.** `tests/test_onset_answer.py` (37) on the frozen Ravi fixture;
`test_pause_tolerance.py` stub updated. Not done: pre-synthesizing the good-time line
while the identity question plays (TTS first chunk is ~0.4 s, left as is).

## 66. Every agent says it is an AI when — and only when — explicitly asked (2026-09-30) — LIVE

`app/voice/ai_disclosure.py` + a router branch ahead of the identity shortcut and the
FAQ playbook. `asks_if_ai()` needs a question SHAPE about the agent (en / romanized /
Devanagari / Gujarati: "are you a robot", "is this a recording", "क्या आप AI हो",
"इंसान हो या मशीन", "તમે રોબોટ છો"); the bare word मशीन in "आपकी मशीन है क्या?" does
not trigger it. Swept against all 9,992 caller lines in `calls.transcript`: 3 hits,
all genuine, 0 false positives — re-run that sweep before widening a pattern.

Answer order: (1) the owner's objection-playbook answer for that wording, if it
admits being an AI (`admits_ai`) — kept verbatim (angelo-outbound, jai_mistry-2);
(2) `response_bank.ai_disclosure_responses` — existed in the schema, never read until
now; (3) the platform line ("Yes, I'm Ravi, an AI voice assistant calling on behalf of
Pramukh Group." / "जी, मैं Ravi हूँ, Pramukh Group की तरफ़ से एक AI voice assistant।").
(2) and (3) then repeat the question the caller was answering. A playbook answer that
denies it ("No, I'm a real person") is overridden. `personality.ai_disclosure` still
shapes the prompt's tone; every mode now says "AI voice assistant" and "never bring it
up yourself" (golden prompts regenerated). Tests: `tests/test_ai_disclosure.py` (45).

## 67. Copilot quick controls: voice + speaking speed by click (2026-09-30) — LIVE

A **Voice & speed** button in the Copilot header (shown once the chat has a draft or an
agent) opens a side sheet: a speed slider (one for all languages, or "Per language"),
a voice grid with female/male filter and a play button per voice, and the resulting
pronouns ("follows the voice"). Save writes `languages.tts_voices.<lang>.{speaker,pace}`.

- `GET/POST /api/copilot/sessions/{sid}/quick-controls` (`agent_copilot_api.py`).
  Target = the chat's agent once it is on disk (both twins by default via
  `_mirror_to_twin`, `both_directions: false` to skip), else the interview draft.
  Same store path as `update_agent` → a revision; pronouns re-derive on load. The
  session's model context gets a "[Quick controls — … ALREADY SAVED]" note so the
  chat does not undo it. Pace 0.5–2.0, snapped to 0.05; only bulbul:v3 speakers.
- Voice change leaves pace alone — speed belongs to the voice (§51); the sheet says so
  and each slider has a live preview at that speed.
- `/api/tts/preview` was not on the tenant allowlist: every non-admin's Builder
  Preview button 403'd. Allowlisted; its Hindi/Gujarati sample line now matches the
  voice's gender.
- `GET /api/copilot/sessions/{sid}` now also returns `interview` / `pending_changes`
  (`_rail_payload`), so the progress rail and diff cards survive a reload.
- Tests: `tests/test_copilot_quick_controls.py` (10) on the frozen Ravi twins.

## 68. Pramukh per-project agents + budget-fit pitch switch (2026-10-07) — LIVE

The Pramukh owner (patelashishk007@gmail.com) asked for one agent per Vapi
project, each "the same as Ravi" but pitching its own project, all sharing one
knowledge base, and able to suggest a project that fits the caller's budget —
switching the pitch only after the caller explicitly agrees.

| Slug family | Agent | Organisation (spoken) |
|---|---|---|
| `pramukh_vedanta-*` | Ravi | Pramukh Vedanta |
| `pramukh_blue_stone-*` | Prashant | Pramukh Blue Stone |
| `pramukh_vivan-*` | Vijeta (simran, 1.15) | Pramukh Vivan |
| `pramukh_aristo-*` | Prashant | Pramukh Aristo |
| `pramukh_silver_stone-*` | Ravi | Pramukh Silver Stone |
| `pramukh_swarit-*` | Akshay | Pramukh Swarit |
| `pramukh_green_county-*` | Dhaval | Pramukh Green County |

- **Source object**: `deploy/pramukh_vapi_projects.yaml` (owner's sheet, typos
  fixed, ambiguities listed at the top and kept out of what the agent may
  assert). **Builder**: `deploy/build_pramukh_projects.py` — reads Ravi's CURRENT
  twins (`outbound_lead_qualifier-*`) and swaps persona, organisation, pitched
  project and catalogue. Stage goals NAME the project but carry no facts.
  Re-run `--apply` inside `lescall-api` after editing the sheet; existing slugs
  go through `store.write_agent` (revision+1).
- **Engine (opt-in)**: `knowledge_base.primary_project.budget_switch: true`
  (`app/voice/pitch_switch.py`). When the budget slot does not fit the pitched
  project's `budget_lakhs` (±15%), a per-turn `━━ BUDGET FIT ━━` band lets the
  agent suggest up to two fitting catalogue projects in ONE sentence. The
  agent's spoken line decides what was offered; the caller's next turn decides:
  a yes (or the offered name) switches, a no — or two unanswered turns — keeps
  the original and silences suggestions until the budget changes. A question
  about another project never switches. After a switch, the per-turn bands read
  a switched VIEW of the definition, the stage goal is re-pointed, and
  `━━ PITCH SWITCHED ━━` overrides the cached static band (which is never
  rebuilt — §62 cache). No flag → byte-identical (golden tests green).
- Tests: `tests/test_pitch_switch.py` (117) on the frozen
  `fixtures/pramukh_swarit_akshay_outbound_2026_10_07.yaml`.
- **Deployed to angelo@dhronai.com ONLY** (owner request, same day), all 14
  rows `is_active=0` (an active row becomes the account's fallback for unbound
  calls). Built from angelo's Ravi copy `outbound_lead_qualifier-2-*`.
- **Ravi's Swarit price** → 4 BHK ~₹2.15 Cr, 5 BHK ₹2.35–3.70 Cr on angelo's
  copy only (`deploy/update_ravi_swarit_price.py`, rev 2). The customer's live
  Ravi (`outbound_lead_qualifier-*`, patelashishk007) still says ₹2.50–3.50 Cr.
- **Confirmation** accepts every positive reply (English / Hinglish / Hindi /
  Gujarati: ok, sure, chalega, ठीक है, बिल्कुल, બરાબર, why not, no problem…).
  Hedges ("sochta hoon", "baad mein") are not a yes; a trailing "ना" is a
  softener; a question about the offer keeps it open without spending its
  two-turn expiry. `classify_answer` order: hedge → positive-with-negation →
  negation → positive.
- Not done: no real-LLM sim call (shared free-tier key).

## 69. "Heavy delay, misheard me, moved on without understanding" — Prashant call 1032 (2026-10-07) — LIVE

Same pattern on the Ravi-copy calls 1028–1031: median speech-end→first-audio
3.4 s, 2–4 slots skipped per call. Platform-wide fixes:

1. **Whole-turn re-transcription** (`pipeline._retx_*`, `TURN_RETRANSCRIBE`).
   The VAD cuts a sentence at every ~300 ms pause and each ~1 s piece was
   transcribed alone: garbled ("की बात कर सर।" for "किसकी बात कर रहा हूँ?") or
   EMPTY (35 STT calls, ~half empty — those words were lost). A turn made of
   ≥2 pieces (empty ones count, unless they overlap our own playback) is now
   transcribed once as joined audio; the request starts speculatively as the
   pieces arrive, so it is usually back before the merge window closes.
   Offline re-transcription of call 1032's caller channel recovered every
   misread line. Log tag `[STT-TURN]`. Tests `test_turn_retranscribe.py`.
2. **A reply that is not an answer does not spend the ask.**
   `_non_answer_kind` (question back / confusion — "कौन था सर?", "किसकी बात?",
   "which one?" / hello) → `observe_user(non_answer=True)` →
   `StateManager._keep_ask_budget` (max 2 per slot). Call 1032 skipped
   calling_from, purpose and family_size on exactly such replies.
3. **YOU ALREADY ASKED THIS no longer says "this is their answer — move on"**
   unconditionally. With a non-answer verdict it says: they did NOT answer, no
   "समझ गया", answer them / clear it up, then re-ask in simpler words; without
   one it tells the model never to pretend it understood a reply that does not
   fit. Tests `test_non_answer_progression.py`.
4. `_line_mentions_name` (name-fatigue band) also counts "श्री/Mr. <name>".

Not changed: provider time-to-first-token (gemini-3.5-flash-lite, 1.3–1.5 s
median all week) and the merge holds (§46 — each one exists for a measured
double-reply / swallowed-speech incident).

## 70. Call 1033 — replies cut before they were heard, own question discarded as echo, "Vapi में" misheard; agents Hindi-only (2026-10-07) — LIVE

1. **Delivery guard for every unheard reply** (`BARGE_GUARD_ALL_REPLIES`).
   Two replies were killed 0.15 s / 0.43 s into playback by echo/line noise
   (relaxed continuation-window threshold); the caller heard nothing and sat
   in 7–10 s of dead air. `_connect_barge_defer` now defers ANY barge-in
   against a reply with < `CONNECT_MIN_PLAYED_S` audible, not only in the
   connect window: real words kill it via the STT verdict, a hello or an
   empty transcript lets it play on.
2. **Echo-text guard**: "कौन बात कर रहे हैं?" was discarded as echo of "…कहाँ
   से बात कर रहे हैं?". Short transcripts (≤6 tokens) must now be covered
   COMPLETELY by the agent's words; and the tokenizer no longer splits on
   `\W` (Python's `\w` excludes Indic vowel signs — "कौन" was shredded).
3. **Whole-turn pass includes trailing pieces** up to commit (a "Vapi में"
   piece returned empty AFTER "हाँ" was dropped); an empty piece is treated
   as echo only if it lies entirely inside our playback.
4. **Place-answer sound-alikes** (`_contextual_city_fix`): after a
   calling_from/city/location question, a ≤4-word reply "अभी मैं" / "बाबी में"
   / "abhi mai" → "Vapi में". Only cities in `knowledge_base.locations`.
5. **Hindi only** for angelo@'s 14 Pramukh agents + Ravi copy
   (`hindi_only()` in the builder; `deploy/update_ravi_copy_hindi_only.py`):
   supported [hi], `stt_forced_language: hi-IN`, no auto/evidence switch,
   `tts_lang_from_text: false`. Call 1033 heard "Vapi में" as "I like it." and
   the agent replied in English. Customer's live Ravi unchanged.
Tests: `test_call1033_hearing.py` (12), `test_connect_hello.py` updated.

## 71. "Ravi test" — Pramukh Vedanta on Pramukh's ideal call script (2026-10-07) — LIVE (angelo@ only)

`pramukh_vedanta_test-{inbound,outbound}` (display "Ravi test", spoken name
Ravi, inactive), built by `deploy/build_pramukh_vedanta_test.py` on top of the
per-project builder. LesCall opening beats kept; then REQUIREMENT_FIRST
(one-clause context + "understand your requirement first" + self-use or
investment; branch: self-use retires `investment_horizon`) →
UNDERSTAND_REQUIREMENT (3/4 BHK → budget → timeline → `decision_factor`,
+ horizon for investors; calling_from / family_size optional) →
FIT_AND_PITCH (purpose- and factor-led; investor: no return promises) →
INVITE_SITE_VISIT (20–30 min, Saturday evening / Sunday morning) → close.
Vedanta's `price_range` carries NO figure (exact unit price + cost sheet);
`budget_lakhs` keeps the budget check working. Objections from the script:
price_question, whatsapp_details, price_high, decide_later,
investment_returns. Follow-up-call behaviour lives in `direction_brief`.
Facts per the recommendation: 5.9 acres, Survey No. 1702, under
construction, 3 & 4 BHK ~1,965–3,355 sq ft, 4 BHK servant room, union of
amenity lists (pool unconfirmed by the owner's sheet). Tests:
`test_vedanta_ideal_script.py` (5). Also fixed: the per-project builder
iterated en/gu voices that the Hindi-only Ravi copy no longer has.

## 72. Pramukh agents: facts are the owner's sheet, and WhatsApp sharing is tracked (2026-10-10) — LIVE

Call 1052 (`pramukh_vedanta_test-outbound`, angelo@, 12.8 min) — the owner's
review: "no major issue … except certain inaccuracies … and the WhatsApp
number". What the transcript showed, and what changed.

**Vedanta facts were a second, older fact-set.** The "Ravi test" builder
(§71) carried its own `VEDANTA` dict from Pramukh's script: 3 & 4 BHK (no
Ultra 4 BHK), "under construction" only, 5.9 acres, Survey No. 1702, a
clubhouse / pool / yoga / toddler zone the sheet never lists. Every wrong
Vedanta line on the call came from it; the seven per-project agents
(`deploy/pramukh_vapi_projects.yaml`) were right all along, and every other
project's answer on the call (Silver Stone, Green County, Vivan, Blue Stone,
Aristo, Swarit — prices, carpet areas, buildings, amenities, addresses)
matched the sheet word for word. Fix: `build_pramukh_vedanta_test.py` now
derives Vedanta from the sheet item (`_vedanta()`), keeping ONLY the script's
pricing policy (no rough figure; exact unit price + cost sheet). The sheet
itself lost two things that were never on it (Swarit "opposite Pramukh
Sahaj" + plot sizes, from Ravi's older brief) and states Vivan as "9
buildings of 14 floors, 3.5 acres". Human copy: `docs/Pramukh Project
Details.md` (grammar fixed, numbers untouched, open points listed) — edit
both, rebuild with `--apply --mirror`.

**The WhatsApp promise was pinned to the pitch and the number question never
settled.** After the budget switch (§68) made Silver Stone the pitch, the
CONFIRM goal's "WhatsApp the {P} location to this same number" was re-pointed
to Silver Stone and the model said it on eight turns while the caller was
asking about five other projects; "इसका address" was answered with Silver
Stone's when the caller meant Green County; "is this number on WhatsApp?"
was asked on eleven consecutive turns (every "हाँ जी" came inside a turn
that also asked a question, so the §61 rule kept the boolean slot empty);
"my WhatsApp is on a different number", said three times, was answered with
"इसी नंबर पर भेज देता हूँ"; the visit was booked for Silver Stone.

- **`app/voice/share_tracker.py`** (opt-in:
  `follow_up.whatsapp_promise.track_projects: true`, set by the builders on
  every Pramukh agent and the test twin). Deterministic, like pitch_switch:
  the catalogue names in each side's line set the TOPIC (last-named wins, so
  "Silver Stone नहीं, Blue Stone" ends on Blue Stone; native-script spellings
  resolve through the agent's own `project_name_corrections`); the agent's
  offer sentence + the caller's yes / send-verb decide what is AGREED (a
  project named BEFORE the send verb is the target, one named after it is a
  new question); "अलग नंबर" / "different number" / "is number pe WhatsApp
  nahi" set the NUMBER to different, a leading yes to the number question
  sets it to same, and a ten-digit number (digits, numerals or spoken digit
  words) is captured only AFTER that. One per-turn band — `━━ WHATSAPP —
  what you share, and where ━━` — says the topic vs the pitch, the agreed
  list, the number status (ask once; never ask again once answered; after a
  different number, ask for it once, confirm the last four digits only), and
  "a visit is for the project THEY name — ask which one" when topic ≠ pitch.
  Goals, playbook and guardrails on the agents now defer to that band
  instead of hard-wiring "{P} to this same number".
- Records: `collected_data.flags.whatsapp_share_projects` (names, in order),
  `whatsapp_topic`, `whatsapp_number_status`, `whatsapp_alt_number` (digits),
  `whatsapp_promised` once anything is agreed; the `whatsapp_same_number` slot
  is written (`source=system`) so CALL STATE agrees with the band.
  `shared/whatsapp/consent.py` reads the alternate number as `given_phone`
  and `stage.py` takes the first agreed project for `{project_name}` — both
  additive, inert without the flags (sending is still OFF, §64).
- No flag → `_share is None`, no band, byte-identical prompt. Tests:
  `tests/test_share_tracker.py` (45, incl. a replay of call 1052) +
  `test_vedanta_ideal_script.py` on the refreshed fixture.
- **Mirroring**: `build_pramukh_projects.py --apply --mirror` rewrites every
  share-imported copy (`pramukh_<key>-<n>-<dir>`: arisha@ Aristo + Blue
  Stone, shubhangi@ Aristo + Green County) with the same definition, keeping
  that copy's `agent_id` and `calendar`; the `agents` row is untouched.
  Revisions: originals 4, mirrors 2 (aristo-3-inbound 3), test twin 2.
- Also on the call: the model wrote "प्रमोद विवान" for Pramukh Vivan once —
  an always-rule now keeps "Pramukh" and project names in Latin letters.

## 73. Pramukh test calls 2026-10-11: identity re-confirmation, a Hindi-only agent answering in English, "I can't come now" re-invited, an investor asked who will live there (2026-10-12) — LIVE

The owner tested the Pramukh per-project agents from three staff accounts
(arisha@: 1057–1059, 1062–1064; shubhangi@: 1060, 1061; angelo@'s copies the
same build) and asked for four things. Each was a platform defect, fixed in
the engine; the builders carry the authored lines and stage wiring, and
`--mirror` rewrote every copy (angelo@ 14 + `pramukh_vedanta_test-*` 2 +
arisha@ 4 + shubhangi@ 6, the new `pramukh_silver_stone-2-*` included; all
mirrors were byte-identical to the originals before the run).

1. **"No" / "wrong number" to the identity question is re-confirmed once,
   and nothing about property follows an unconfirmed identity.** Before:
   the bare "नहीं" went to the model with `wrong_person_response` quoted,
   and the next turn introduced the offer and started discovery (call 966
   on the customer's live Ravi — 10 such declines in the corpus). Now,
   outbound only, ahead of the objection branch: a bare no (`_is_identity_no`,
   every token from a no/filler vocabulary — "कौन?", "नहीं सुनाई दिया" and
   "नहीं मैं बिज़ी हूँ" do not match) right after the identity beat, or a
   wrong-number phrase anywhere (`_WRONG_NUMBER_RE` + the agent's
   `wrong_number_patterns`) → `outbound.wrong_number_reconfirm_line` (built-in
   per language; the Pramukh builders author the owner's wording: "क्या आप बस
   एक बार confirmation के लिए वापस बता सकते हैं कि ये ग़लत नंबर है या सही?").
   The answer: a yes / acknowledgement / repeated no → `wrong_number_close`
   ("तकलीफ़ के लिए माफ़ कीजिएगा, आपका दिन शुभ हो।") + hangup, outcome
   `wrong_number`; "मैं ही हूँ / सही नंबर है / <contact name>" → the authored
   good-time line, no model turn; "वो नहीं हैं / मेरे पति का नंबर है" →
   `wrong_person_response` + a per-turn `━━ IDENTITY NOT CONFIRMED ━━` band
   (no pitch, only when the contact is reachable; `extra_flags.wrong_person`);
   "who is this?" → identity + the question again; anything else → asked once
   more, then closed. Verdict order is me → wrong-number phrase → someone
   else → acknowledgement ("मैं वो नहीं हूँ" must not read as "someone else").
   The pending ask is on the turn-transaction snapshot (a continuation merge
   forgets it). `WRONG_NUMBER_RECONFIRM=false` restores the pre-§73
   immediate close on a phrase.
2. **A Hindi-only agent answered "3 bhk" in English (1059, 1063) and closed
   1064 with "You're welcome!".** The log: `[LANG] "3 bhk" -> en …
   Switching to English (ascii multi-word, words=2, min=2)`. §70's
   `hindi_only()` sets `evidence_switch: false`, which routes every turn
   into the LEGACY switcher — the one path that never checked
   `_switchable_languages()` or `languages.auto_switch` (the A2 switcher
   did). Fixes, platform-wide: a clamp after BOTH branches (an automatic
   switch is refused when auto_switch is off or the target is outside
   `languages.supported`; a language seeded from outside the build is pinned
   to the default); the pipeline's STT mirror (`language_allowed`) and the
   "repeat in X" path are bounded the same way; digits and unit/domain
   tokens (`_LANG_DOMAIN_NEUTRAL_TOKENS`: bhk, lakh, cr, sq ft…) never count
   as English; gratitude has hi/gu built-ins (`_GRATITUDE_DEFAULTS`).
3. **"I can't come now" was answered with the INVITE script's "photos and
   videos only show so much… weekend or weekday evening?" — again and again**
   (1057, 1058 "आप क्यों बार बार बोल रहे हो?", 1059, 1060, 1061, 1063), and
   "अभी नहीं" — also a universal objection pattern — got the canned "ज़्यादा
   टाइम नहीं लूँगा, कब कॉल करूँ?" deferral (1060, 1061). Now
   `_detect_visit_decline` runs before the objection branch: STRONG forms
   stand alone ("नहीं आना", "नहीं देखूंगी", "can't come"); WEAK forms ("अभी
   नहीं", "बाद में", "कंफर्टेबल नहीं", "फ्री नहीं", a bare short no, "later",
   "will let you know") only when the agent's previous line invited a visit
   (`_VISIT_INVITE_CUE_RE` + the agent's own visit-slot questions); "पहले
   बताओ / tell me first" is `answer_first` — one turn of `━━ ANSWER FIRST ━━`,
   nothing recorded; a concrete alternative day/time ("सुबह नहीं आ सकते",
   "not Saturday, after 5", "14th October") is a NEGOTIATION, not a decline.
   A decline writes `site_visit_interest` (Maybe later / No — a hard No never
   softens), sets `extra_flags.visit_declined`, downgrades the objection
   strike unless the CALL was declined in the same breath
   (`_CALL_LEVEL_DECLINE_RE` / DND), and arms the per-turn
   `━━ VISIT DECLINED ━━` band (this reply: accept, "I'll send the details on
   WhatsApp" per the WHATSAPP note, answer what they asked; later turns:
   never invite again, follow-up time once). The Pramukh builders add
   `when_slot: site_visit_interest` branches so INVITE_SITE_VISIT exits to
   CONFIRM_AND_CLOSE at once, rewrite the INVITE goal and the `send_brochure`
   guidance ("ask about a visit once, lightly, unless a VISIT DECLINED note is
   present"). Dead-air repair (`_last_question_asked(answered_stop=True)`)
   no longer re-asks a question the caller already answered — 1059 replayed
   the refused invitation after the caller had agreed to WhatsApp. Corpus
   sweep: 19 detections in 10,830 caller lines, every one a real decline or
   a "tell me first"; the first draft's loose weak rule had 30 false
   positives (corrections like "4 bhk नहीं, 3 bhk", "Blue Stone भी तो देखना
   है") — re-run the sweep before widening a pattern.
4. **Investment recorded on turn 4, "और वहाँ कौन-कौन रहेगा?" still asked**
   (1057, 1060 "यह इन्वेस्टमेंट है तो फिर मुझे नहीं पता कौन कौन रहेगा", 1064).
   The engine now runs ACTION-ONLY branches (`actions` with no `goto` — used
   to be skipped with a warning): the builders add `purpose = Investment →
   retire_slots:family_size` to LET_THEM_TALK and SOFT_QUALIFICATION, and the
   goals say an investor is never asked. 1060 also flipped a recorded
   Investment to Self-use on "वहाँ पर रहने के लिए क्या-क्या एडवांटेजेस हैं?" —
   `purpose` is now `correction_gated`, and `_CORRECTION_MARKER_RE` had `\b`
   around `नहीं`, which never matches before a comma or space (Python's `\w`
   excludes the anusvara) — a correction_gated slot could not be corrected in
   Hindi at all. Script-aware edges now.

Not changed, noted: "मैं आपसे पूछ रही हूँ व्हाट्सएप पे नहीं चाहिए" (1063) still
hits the deferral canned line via "नहीं चाहिए"; a "call me tomorrow at 11"
after an invitation (1061) is a callback, handled by that path.

Tests: `test_identity_reconfirm.py` + `test_hindi_only_language.py` +
`test_visit_decline.py` (127 together); `test_stage_machine_v2.py` /
`test_agent_hangup.py` updated to the two-step close. 881 related tests
green. Deploy: both builders `--apply` (`--mirror` on the per-project one),
`lescall-api` then `lescall-runtime`.

## 75. "Ravi test" on the owner's scripted sales pitch — Pramukh Vedanta, line by line (2026-10-12) — LIVE (angelo@ only)

(§74 is reserved for the parallel owner-review of the Aristo agent — "pitch
v2" behind `PITCH_V2` in `deploy/build_pramukh_projects.py`,
`deploy/build_pramukh_aristo_test.py`, call 1071 — worked in another
session the same morning.)

The owner sent the Vedanta pitch as a script: opening beats unchanged; ONE
authored requirement line ("Pramukh Vedanta में हम 3 और 4 BHK homes लेकर आए
हैं, New Chala, Vapi में — तो आप ये property ख़ुद रहने के लिए देख रहे हैं, या
investment के लिए?"); four questions in their words, one per turn (3 or 4
BHK → approximate budget range → immediate or next 3–6 months → the most
important factor), each STORED, never re-asked, and never filled from a
misheard fragment; the pitch by purpose (self-use: "Perfect Sir. आपकी
requirement समझने के बाद… <project> … <configurations> residences …
<amenities> … <ready / under construction> … site visit काफ़ी useful
रहेगी"; investor: "Understood Sir. … कोई unrealistic return promise नहीं
करूँगा … आपका investment horizon approximately कितना है — 3 से 5 years या
long-term?"); "क्या आप <project> के बारे में थोड़ा और जानना चाहेंगे, या आपका
interest किसी और project में है?"; open Q&A with four verbatim answers
(price / "WhatsApp kar do" / "price high hai" / "baad mein dekhunga");
after EVERY answer "क्या आपके पास इस property के बारे में और कोई सवाल या
doubt है…?" until the caller says no; the 20–30 minute visit invitation
ending "क्या कोई date और time है आपके दिमाग़ में site visit के लिए?"; the
day/date/time read back ONCE as "बस confirm करने के लिए एक बार और बोल देता
हूँ: …"; their yes; "ठीक है जी, धन्यवाद। जाने से पहले क्या मैं आपकी और किसी
चीज़ में मदद कर सकता हूँ?"; a doubt is answered and the line repeated; "no"
→ "Thank you, आपका दिन शुभ हो।" and the call ends — nothing in between.

Everything lives in `deploy/build_pramukh_vedanta_test.py` (the YAML is
never edited by hand; `--apply` → `pramukh_vedanta_test-{inbound,outbound}`
on angelo@, inactive, revision 9). The lines are native script with the
English words in Latin (CLAUDE.md: Sarvam degrades on romanised Hindi) and
"Sir" is left to the gender filter (§42). Stages: WARM_WELCOME →
INTRO_AND_TIME_CHECK → REQUIREMENT_FIRST (purpose; the value retires
family_size / investment_horizon) → UNDERSTAND_REQUIREMENT (configuration,
budget, timeline, decision_factor; max 8 turns) → FIT_AND_PITCH
(`min_turns: 1` so the cascade cannot skip the pitch; collects the horizon,
retired for self-use) → KNOW_MORE (`when_intent: no_more_questions` →
INVITE_SITE_VISIT) → INVITE_SITE_VISIT (appointment_captured; a declined
visit branches to the close, §73) → CONFIRM_VISIT (`intent: affirmation`)
→ CONFIRM_AND_CLOSE. `termination.confirm_before_end: false`,
`termination.pre_close_question`, `closing_responses` = the owner's goodbye.

### 75.1 Four engine capabilities, all opt-in (platform byte-identical otherwise)

* **`slots[].ask_gated`** (`schema.SlotDefinition`, `state_manager.SlotSpec`,
  `merge_state`): a write from regex / the LLM gap-filler / the record tool
  lands only when the agent's LAST line asked that slot — `_just_asked`:
  the utterance hint or the directive noted on the previous turn, never the
  head-of-missing fallback of `_is_being_asked` (with it, "हमें 3 BHK
  चाहिए, location बताओ" filled the size and, in the same breath,
  decision_factor=Location) — or when the utterance carries a correction
  marker. `source="answer"` / `"system"` are exempt. Why: call 1052's
  decision_factor flipped SIX times from questions about the project
  ("लोकेशन बता सकते हो" → Location, "एमेनिटीज़ के बारे में बता सकते हो" →
  Amenities, "कंस्ट्रक्शन में है अभी?" → Construction quality) and the LLM
  gap-filler wrote it from "निवेश के लिए" two turns before it was asked. On
  the test agent: purpose (plus correction_gated), purchase_timeline,
  decision_factor, investment_horizon. Configuration and budget stay free
  (a volunteered "3 BHK चाहिए" is a value; the money check already guards
  the budget).
* **`branches[].when_intent: no_more_questions`** (`_no_more_questions_goto`,
  checked just before the farewell/close branch): the caller's done-reply
  to the stage's "any other question?" (`_is_close_confirmation` — the
  same test as a close permission) takes the branch instead of closing the
  call; `max_takes` as for any branch. Without such a branch the reply
  closes exactly as before.
* **`call_flow.termination.pre_close_question`** ({lang: str}): the
  owner's "anything else before I go?" line. `_CLOSE_PERMISSION_RE` learned
  "और किसी (चीज़|चीज|बात)" / "aur kisi (cheez|chiz|baat)" — bound to the
  noun so "और किसी दिन आ सकते हैं?" is not a close — and
  `visit_confirmed_block(close_question=…, close_asked=…)` tells the model
  to say exactly that line (and wait) when the caller confirms the
  read-back, instead of the band's goodbye-with-[END_CALL]; once it has
  been asked (`_close_question_asked`, last two agent lines) the goodbye
  bullet is back. Registered in `controls.KNOWN_LEAF_KEYS` and the Copilot
  normaliser/schema doc.
* **A budget-fit alternative must offer the caller's configuration**
  (`pitch_switch.offers_config`, `alternatives(config=…)`, the router's
  `_pitch_config_value()` → `band(…, config_value)`): "Ultra 4 BHK" counts
  as a 4 BHK; an item with no `configurations` list is never hidden.

* **`call_flow.stages[].script` — the engine's floor under the model**
  (`_script_followup`, spliced after the streamed reply, before it is
  stored): `ask_verbatim: true` speaks the slot question the directive
  asked, word for word, when the reply did not ask it (content-word
  overlap ≥ ½ with the authored line, or one of the slot's own question
  signals inside a question, counts as asked — a paraphrase is not
  repeated); `after_answer: {lang: line}` follows every reply of the
  stage unless the reply already carries it, asks a permission-to-close
  question, or carries one of the `unless` lines; nothing on a closing
  turn. The test agent: `ask_verbatim` on the three requirement stages,
  the doubts question after every KNOW_MORE answer (unless the stage's
  opening question, the WhatsApp line or the "later" line), the pre-close
  line after every CONFIRM_AND_CLOSE answer (unless the goodbye). The
  appended text joins `last_agent_response`, so the no_more_questions
  branch, the QA tracker and the ask-budget reconcile all see it. Needed
  because the model, told "verbatim" in three places, still pitched in
  the requirement stage and asked the doubts question there (sim run 3).
* **An authored pre-close line is THE close permission.** With
  `confirm_before_end: false` a "नहीं बस" to the doubts question in the
  requirement stage hung the call up (sim run 3) — `_is_close_confirmation`
  now requires the last line to carry `pre_close_question` when one is
  authored; `_is_done_reply` (the generic test) serves the
  no_more_questions branch, which also looks one agent line back (an
  OBJECTION answer the model ended with its own question sat between the
  doubts question and the caller's "no").

### 75.2 Platform bugs found on the way (fixed for every agent)

* **`_DONE_REPLY_RE` ended in `\b`** — "नहीं" ends in a vowel sign +
  anusvara, which `\w` excludes, so `नहीं\b` never matched: a Devanagari
  "नहीं" / "ना" to "और कुछ?" was never a deterministic close (it went to
  the model). Script-aware edge now, same class as §73's
  `_CORRECTION_MARKER_RE`.
* **`_reconcile_ask_budget` trusted the keyword matcher over the QA
  tracker.** `_note_ask_signals` scans a reply for slot NAMES and LABEL
  words — on the authored line "…3 और 4 BHK homes… investment के लिए?" it
  credited investment_horizon (the word "investment" in that slot's label)
  and un-noted the purpose ask the line had just voiced, so the caller's
  "investment के लिए देख रहा हूँ" was refused as "not asked" (first sim run
  of the day: purpose, timeline, factor and horizon all stayed empty and
  the flow never left the requirement stage). The QA tracker matches the
  slot's OWN question text (§45.5 widening) and read every authored line
  correctly (opening → purpose, Q_FACTOR → decision_factor, the pitch →
  investment_horizon). The tracker's reading now wins in
  `_reconcile_ask_budget` and as the `asked=` hint to `observe_user`; the
  keyword scan is the fallback only when the tracker found nothing.
* **A tool booking advanced the waiting stage one turn late.**
  `book_appointment` writes CallState after the reply and the mirror into
  `memory.booking` ran only at the next prompt build, so the stage gated
  on `appointment_captured` advanced a turn after the booking and that
  turn's reply came from the old goal (a second invitation instead of the
  read-back). `_maybe_advance_stage` mirrors first now.
* **`_CLOSE_PERMISSION_RE` had "और कोई" but not "कोई और"** (the Latin
  "koi aur" was there) — added, with the model's "किसी और चीज़" paraphrase.
* **Dead-air repair treated "hmm" as an answer** (§73's `answered_stop`):
  a bare filler turn no longer stops the scan, so the open question is
  replayed, not "are you still there?" (`test_tool_turn_speech` was red).

### 75.3 Known and deliberate

* The owner's pitch template listed clubhouse / swimming pool with kids'
  pool / yoga & meditation / toddler zone / 4 BHK with a private servant
  room. The sheet (§72, the owner's own re-send after call 1052) lists
  other amenities for Vedanta, so the template takes `<amenities>` from the
  KNOWLEDGE BASE and the owner was told. The sheet also says only the
  Ultra 4 BHK is available now; the script's "3 और 4 BHK" and "3 BHK या 4
  BHK?" are kept as written — availability is answered from the sheet.
* "बाद में देखूँगा" BEFORE any invitation gets the owner's verbatim answer
  ending "weekend better रहेगा या weekday?"; after an invitation it is a
  visit decline (§73) and the VISIT DECLINED note wins (details on
  WhatsApp, no second invitation) — the guidance says so.
* A corrected day/time after the read-back is spoken but the booking is
  fill-only (`book_appointment` refuses a change once fixed) — pre-existing.
* `test_copilot_control_surface.py::test_every_live_agent_key_is_known_or_provably_dead`
  still fails on `nishaa_ai-*` (`guardrails.compliance.dnd_phrases`, written
  by another session the same morning) — not this change.

Tests: `tests/test_vedanta_scripted_flow.py` (23 functions, 35 cases); the
full backend suite is green except two pre-existing failures (the
`nishaa_ai-*` keys above, and `test_llm_payload_contract` reading the
deleted `kaapd_pooja` agent). Deploy: builder `--apply` (revision 9),
`lescall-api` then `lescall-runtime`. Verified
with the scratchpad real-engine sim (`run_sim.py`, SIM_DIRECTION=outbound):
investor and self-use paths — see the session report.

## 76. Green County (Dhaval) owner review — amenities are not an inventory, unit/floor availability is not published, a refused booking is never "booked" (2026-10-12) — PENDING OWNER APPROVAL, NOT DEPLOYED

The owner's behavioural rules for Pramukh Green County (rules 11–15: no
fabricated project facts, verify a tool's result before confirming it) were
audited against the data and the engine in the morning session; four
findings, F1/F2 approved, F4 approved as the smallest patch, F3 (other
projects' prices in the same prompt) ON HOLD — no `hide_others_price`, no
catalogue entry removed, the shared knowledge base untouched. The session
that held the approval died at 07:50 after writing F4's engine code and its
tests without reporting; this section is the reconstruction and completion.
Nothing here is applied: no `--apply`, no regeneration, no restart.

### 76.1 F1 — an amenity is never presented as built and in use (engine)

The sheet lists 27 amenities for Green County with no status and the project
is "under construction and ready to move, both"; `amenities` renders as a
flat "Amenities:" line and the model reads a list as an inventory ("you have
an indoor swimming pool and a Dolby theatre"). `catalog.amenity_status_clause`
appends ONE built-in sentence to the item's line — "Amenities are what the
project offers or plans — do not say any of them is built, open or in use
unless Status says so; say it is part of the project." — ONLY when the item
carries `amenities` AND its `availability` or `status` says under
construction (`_UNDER_CONSTRUCTION_RE`). Every other item is byte-identical:
rendered offline against the sheet and every frozen fixture catalogue, 107
items, 91 unchanged and the 16 changed are the four unfinished projects.

**Blast radius (engine-level by design):** the shared Pramukh catalogue is
carried by all 28 Pramukh agents, and four of its seven projects meet the
condition — Green County, Vedanta, Blue Stone, Silver Stone — so the clause
appears on those four lines in every Pramukh agent's knowledge band the
moment `lescall-runtime` restarts; Vivan, Aristo, Swarit and every non-Pramukh
agent render exactly as before. `tests/test_catalog_amenity_status.py` pins
that set; if it changes, the owner edited a project's availability.

### 76.2 F2 — unit-wise and floor/tower availability are NOT published (data)

Green County's `not_published` in `deploy/pramukh_vapi_projects.yaml` gains
`unit-wise availability` and `floor and tower availability` (the sheet has
no per-unit inventory, so "the 3 BHK is available" could only be improvised).
The builder copies the item verbatim into `knowledge_base.catalog` and
`catalog._DETAIL_KEYS` renders it as "NOT published — never guess: possession
date for the under-construction buildings, RERA number, unit-wise
availability, floor and tower availability." `docs/Pramukh Project
Details.md` open point 7 is the human-readable copy. The pre-change sheet is
frozen as `tests/fixtures/pramukh_vapi_projects_2026_10_10.yaml`, and
`tests/test_green_county_not_published.py` pins the diff: Green County gains
exactly those two items, the other six projects are deep-equal and render
identically. Dry run (`--dry-run --only green_county`, writes only
`deploy/agents_dry/`): the generated twins differ from the live ones by the
two lines — plus `script: {}` on every stage and `ask_gated: false` on every
slot, which are §75's new schema defaults being serialised on any
regeneration (inert) and not part of F2.

### 76.3 F4 — a spoken booking claim the gates refused (engine)

Execution path in "on" tool mode: `_call_llm_stream` streams the model's
words to the caller as they arrive and only collects tool calls; after the
stream `_execute_tool_proposals(had_text)` runs the gates (never books /
missing args / `_is_bookable_when` / past date / closing or turn ≤ 1 /
already fixed). The model's own words were the confirmation whenever it
spoke, and nothing reconciled them with the gate — reproduced with a stubbed
LLM: "Perfect, I have booked your site visit for tomorrow at 4 PM" was heard
on all four cases (valid, `today`/`now`, a past year, closing stage) and
only the valid one was recorded. The tool-only re-ask was worse: its trailer
told the model "the customer's details have already been recorded".

Smallest fix, as approved: the executor records `_booking_refused =
(turn, "unusable"|"refused")`; `_booking_unconfirmed_reason()` holds it for
that turn and the next (and clears once a visit is fixed); the prompt gains a
per-turn `━━ NOT BOOKED ━━` band (`prompt_blocks.booking_unconfirmed_block`:
"unusable" → ask for a real day and time, "refused" → do not offer or confirm
a booking); and the re-ask trailer says "No booking was recorded… do not say
a visit or appointment is booked" instead of "already been recorded". The
spoken claim cannot be unspoken — the next turn is the only place to correct
it. WhatsApp is NOT in this path: there is no WhatsApp tool in `llm_tools.py`;
the agent promises ("I'll send…"), `shared/whatsapp/stage.py` sends after the
call, and the sender re-checks consent / IST window / daily cap (§64). The gap
that remains is product-level — a promise made on the call is not tied to a
send that is later deferred or refused — and was left unchanged on purpose.
Tests: seven in `test_tool_turn_speech.py` (`# ── F4`), two of them on the
re-ask PROMPT itself through a recording client.

### 76.4 Not addressed here

Dhaval repeating questions, rushing and ignoring requests: a separate
investigation from call traces, after this change is reviewed. F1/F2 do not
claim to fix any of it.

Deploy, once approved: `docker exec -i lescall-api python - --apply --only
green_county < deploy/build_pramukh_projects.py` (and `--mirror` for the
shubhangi@ copies), then `docker restart lescall-api`, then
`docker restart lescall-runtime` — F1 and F4 are engine code and need only the
restarts; they reach every agent.

## 77. Green County (Dhaval) test calls 1060/1061/1073: Swarit came from the customer memory, Hindi callback requests were invisible, the good-time beat was rolled back (2026-10-12) — LIVE since 14:10 UTC (loaded by a sibling session's restart; owner-approved for test)

Investigation: `docs/reports/green_county_outbound_investigation_2026-10-12.md`.
Three engine fixes, all platform-wide, none applied to a container; no agent
regenerated. The budget switch, honorific behaviour and F3 are untouched.

### 77.1 A primary-project agent is not shown other projects' history (RC1)

`customer_memory` is kept per owner + contact and shared by every agent of
that owner. Call 1047 (the Green County agent itself) switched its pitch to
Swarit on a ₹2.5 Cr budget and booked a Swarit visit; the intel pipeline wrote
"Pramukh Swarit bungalows (primary)" as a preference, and calls 1060/1061
opened with "आपने हमारे Pramukh Swarit bungalows में interest दिखाया था" —
the ━━ KNOWN CUSTOMER ━━ digest said so while the INTRO goal said "they'd shown
interest in one of our projects". The primary pin, exclusive mode and the
MATCH RULES band all held. `prompt_blocks.scope_digest_to_primary` now
removes, item by item, every digest entry that names a catalogue project
other than the agent's primary (the script-aware `pitch_switch.mentions`),
and the band carries one line: this call is about {primary}; never name
another project as its reason. `customer_context_block(ctx, agent_def)`;
`set_customer_context` logs what was withheld. The memory row, the post-call
pipeline and every agent without a primary are byte-identical.
Tests: `test_precall_context.py` (§77 block).

### 77.2 Callback requests in Devanagari / Gujarati (RC2)

`_CALLBACK_ASK_RE` was Latin-only and the weak time-word branch required a
Latin call/baat/phone: "पांच मिनट में कॉल बैक कर सकते हैं" reached the model
as `general` (family size asked next, twice), "बाद में कॉल कर सकते हो?" and
"कल ग्यारह बजे कॉल कर दीजिए" classified `objection` and got the canned
deferral; nothing was recorded. `_CALLBACK_ASK_INDIC_RE` adds the REQUEST
shapes (कॉल बैक; बाद में / थोड़ी देर बाद + कॉल|बात; मुझे … कॉल + a request
verb; a bare imperative; a time word + कॉल; a day part + बात करते/करेंगे;
Gujarati મને … કૉલ કર, પછી કૉલ કર), each refusing मत/नहीं, past and
received forms — never `\b` next to Indic text. Corpus sweep 2026-10-12
(11,621 caller lines): 36 hits before, all Latin; 63 after — the 27 new ones
all Devanagari, 25 requests or deferrals, one "I will call you" and one
narration of a request, both tolerated; nothing lost.
`tests/test_callback_indic.py` pins both lists. With the
detector working, the built-in clarification and confirmation lines
(`_clarify_question`, `_clarify_confirmation`, `_offset_phrase`, the
`_handle_callback_request` fallback) now speak native script for hi/gu with
localised day and time words (`_spoken_day_time`) — they were romanized
Hindi and English — and the busy flavour prefers the agent's authored
`outbound.busy_response`. A per-turn ━━ CALLBACK REQUESTED ━━ band
(`prompt_blocks.callback_pending_block`) renders while
`extra_flags.callback_requested` is set and the call is not closing, so an
unparseable answer to the one clarification question no longer hands the
model back to qualification. Known limit left alone: `_parse_day_time` does
not read Hindi number words ("ग्यारह बजे"); the day is captured, the hour
is asked for.

### 77.3 The good-time beat is never rolled back (RC3)

Call 1061: the fast path spoke the authored good-time line; "ठीक है?" landed
while it played; the continuation merge rolled the provisional turn back
(stage to GREETING, `_good_time_spoken` restored, the line gone from
history), the merged "हां जी। ठीक है?" failed `_is_identity_yes` on the "?",
and the model re-introduced itself from the INTRO goal. The fast path and
the "it's me after all" resume now call `commit_pending_turn()` before
speaking, as closing turns already do; the fragment becomes the next turn.
Tests: `tests/test_good_time_commit.py`.

Deploy, once reviewed: `docker restart lescall-api` then
`docker restart lescall-runtime` — engine code only, no builder run needed.

## 78. Vedanta test call b5452b23: "the VAD cuts me off" was a 400-character cap, a back-channel taken as a turn, and a transcript that hid what the engine heard (2026-10-12) — LIVE on the test twin only (revision 10)

The owner tested the §75 agent (`pramukh_vedanta_test-outbound`, revision 9,
8 min, 11:00 IST) and reported: the pitch "cut off" mid-sentence, "I said
'…इंटरेस्ट है, 4 bhk' but it got cut off", the agent interrupting and
talking over them, and long pauses producing two separate replies. They
asked for the agent to stop its TTS when the caller resumes after a pause,
to merge the fragments into ONE turn under a time threshold, and to apply
it to this test agent first.

### 78.1 What the log actually showed

1. **02:25 — the pitch ended "…एक site visit..."** `llm_config.
   max_response_length` is 400 for every agent and `_validate_response`
   looked for `.`/`!`/`?` as a sentence end — never the daṇḍa — so a Hindi
   reply over the cap was sliced at character 400 mid-word with "..."
   appended, and the TTS spoke exactly that. The owner's lines are 255–647
   characters (PITCH_SELF 647, PITCH_INVEST 440, A_PRICE_HIGH 415, L_INVITE
   372, A_WHATSAPP 366, A_LATER 371): five of them could never be spoken
   whole. No audio was cut.
2. **02:34 — the caller's one-word "हाँ" nine seconds into the pitch was a
   TURN.** `_tts_playing` goes False when SYNTHESIS ends (`tts done` at
   5.4 s into a ~22 s pitch); FreeSWITCH keeps playing the WAVs — the
   sink's `playback_busy_until` said 14.5 s remained. The engine took the
   nod as the caller's reply, FIT_AND_PITCH advanced to KNOW_MORE on it,
   and Q_KNOW_MORE queued behind the still-playing pitch.
3. **05:46 — "4 BHK" WAS heard.** The pieces came back as "हाँ, मुझे
   प्रमुख परिष्टों में इंटरेस्ट है।" + "और भी अच्छे।" (rejected as low
   quality); the §69 joined re-transcription read "हाँ, मुझे प्रमुख Aristo
   में इंटरेस्ट है, फोर बीएच के।" and that is what the engine answered.
   The audit transcript recorded the first piece only — the owner read it
   and concluded the words were lost. (The reply was a verbatim repeat of
   the earlier Aristo answer — the model, not the engine.)
4. **05:52 — the caller spoke over the first 0.5 s of that reply** (1.3 s
   of speech at 10× the floor). The delivery guard deferred the barge-in
   (`played=0.00s`, awaiting an STT verdict); the reply's `finally` cleared
   the deferral; the captured audio was "preserved through cooldown" by the
   echo gate for 10 s, reached STT as a 2.8 s segment and came back EMPTY,
   and the second-consecutive-empty damper spoke "माफ़ कीजिए, ठीक से सुनाई
   नहीं दिया". The caller heard: talked over, then "couldn't hear you".
5. **Q_TIMELINE was never asked.** `[STATE] purchase_timeline: None →
   'Immediate' (source=regex turn=2)` from "हाँ, अभी बात कर सकते हैं" (yes,
   we can talk NOW). The slot is ask-gated (§75); the gate let it through
   because `_note_ask_signals` treats any slot with "time" in its NAME as a
   clock slot and credits a line containing "समय" — the good-time line says
   "आपने कुछ समय पहले…" — so the hint said the timeline had just been asked.
6. **04:49 — a caller QUOTING the agent's price rewrote two slots.** "यह जो
   आपने price range बोला 3 bhk का ₹70 लाख टू ₹85 लाख, यह किसका है?" wrote
   `configuration_required` 4 BHK → 3 BHK and `budget_range` 90 → 85 lakh
   (both `merge_policy: free`), and the agent then cited "आपका जो 85 लाख का
   budget है".
7. **The doubts question stacked behind a question** — 03:22 "…आप अपना नया
   budget क्या रखना चाहते हैं? क्या आपके पास…", 03:41 "…क्या आप उनके बारे में
   जानना चाहेंगे? क्या आपके पास…": the `after_answer` floor did not notice
   the reply was itself a question.
8. **The visit was read back without a time and never booked.** "मुझे
   wednesday का कर दो" → "बस confirm करने के लिए…: Wednesday, 14 October को
   site visit." No time; the caller's "पाँच बजे" came back as "पंजे";
   `book_appointment` was proposed twice with invented times (11:00, 10:00)
   and rightly rejected. No appointment row exists for the call.
9. Smaller: Q_FACTOR voiced four times (max_asks 2 + the §69 non-answer
   grace of 2, each time the caller asked something back); the model
   answered its own factor question when the caller said "हम्म"; "Aristo"
   arrived as परिष्टों / रिस्टो / रिश्तों (one answer began "रिश्ते की बात
   नहीं है"); KNOW_MORE hit its 10-turn escape mid-conversation and the
   invitation came abruptly; the good-time line was the model's variant
   because the identity answer was STT noise, not a plain yes.

Every split in this call WAS merged (nine `Merging split segments` lines,
gaps 1.2–5.6 s, the window re-arming while the caller was audibly still
talking). Nothing was answered twice. What failed was not the VAD.

### 78.2 What was built — `call_flow.turn_taking` (opt-in, this twin only)

Five keys, read by the pipeline through `conversation._turn_taking_cfg()`
(`_tt_cfg` / `_tt_num` / `_tt_flag`); absent = today's behaviour for every
other agent, byte-identical.

| key | Vedanta test | what it does |
|---|---|---|
| `pause_hold_ms` | 1500 | `_pause_may_continue` returns `"agent"` for EVERY caller pause; the merge window holds `max(platform 900, this)` and re-arms while speech is in flight. The owner's "time threshold": a silent gap up to 1.5 s is the same utterance. Costs that latency on every reply. |
| `max_merged_fragments` | 6 | pieces one utterance may be rebuilt from inside that hold (platform 4). |
| `backchannel_guard` | true | a bare acknowledgement (`_is_bare_ack_text`: हाँ / जी / हम्म / ओके / ठीक है / okay sir …, ≤4 tokens, never a negative) whose speech STARTED with ≥ `BACKCHANNEL_MIN_REMAINING_S` (1.0) of our audio still to play is absorbed: logged, no turn, no stage advance. Nobody answers a yes/no a full second before it ends. |
| `sustained_barge_ms` | 700 | a barge-in the delivery guard deferred is carried out once the caller has kept producing loud speech this long past the deferral (`_deferred_barge_override_due`); a "hello?" is under 400 ms. |
| `tail_barge_in` | true | caller speech that is not a nod or a hello, arriving while our audio is still playing after synthesis ended (`_tail_cut_due`: ≥ 0.8 s left), calls `sink.interrupt_playback()` — uuid_break — instead of queueing the reply behind the tail. `_last_reply_completed_clean` is cleared so the stale-fragment rule does not absorb the interrupting speech. |

`_agent_audio_remaining(ts)` = `sink.playback_busy_until − ts`, the one clock
that outlives `_tts_playing`. The continuation merge (cut the in-flight
reply, roll back, re-run one merged utterance) was ALREADY on platform-wide
(`ENABLE_CONTINUATION_MERGE: "true"` in docker-compose); it did not need to
fire on this call.

### 78.3 Platform fixes (every agent)

- `_validate_response`: "।" and "॥" are sentence ends. An over-long Hindi
  reply is now cut at its last full sentence, never mid-word with "...".
- `_note_ask_signals`: clock words ("समय", "बजे", "time") credit a slot only
  when it HOLDS a time (`type: time`, or a non-enum named for time); the
  same for date words. An enum named `purchase_timeline` is not a clock.
- The script floor (`_script_followup`): `after_answer` is never appended
  to a reply that ends on a question of its own; `ask_verbatim` spends the
  slot's own `max_asks`, never more.
- A bare filler ("हम्म", "अच्छा", "okay") to a scripted question on an
  `ask_verbatim` stage gets the question again in the owner's words with
  no model turn (`_scripted_filler_reask`, next to the mid-call-hello
  presence check). The goal text "NEVER answer it on their behalf" was not
  enough: the revision-10 sim still produced "समझ गया, तो location और
  connectivity आपके लिए सबसे ज़्यादा मायने रखते हैं…" on "हम्म". The re-ask
  counts as the slot's second ask, so a third never follows.
- `_CORRECTION_MARKER_RE` knows "चेंज" / "चेन्ज" / "चैंज" / "बदलकर" /
  "बदलना": with `budget_range` correction-gated, "मेरा बजट चेंज करके 70
  लाख से 90 लाख कर दो" was refused on the sim — only the Latin "change"
  and the bare stem "बदल" were markers.
- The audit transcript says what the engine heard: `_retx_apply` emits
  `user_retranscribed` {pieces, text, span}; the bridge calls the new
  `CallAuditSession.replace_last_events(speaker, texts, new_text, span)`,
  which truncates back to the first piece's offset and writes the joined
  text (no-op if anything was written since). `transcript_tail` mirrors it.

### 78.4 Agent changes (builder only)

`deploy/build_pramukh_vedanta_test.py`: the `turn_taking` block above;
`llm_config.max_response_length` 400 → 760 and `max_tokens` 160 → 320 (the
longest line is 647 characters); `configuration_required` and
`budget_range` → `merge_policy: correction_gated` (a stored size or budget
changes only on "change करके / नहीं / actually…"); STT spellings परिष्टों /
परिष्टो / परिस्टो / अरिष्टो / अरीस्टो → Aristo, "प्रमुख रिस्टो" → Pramukh
Aristo; KNOW_MORE `max_turns` 10 → 16; UNDERSTAND_REQUIREMENT goal: "if they
only say hmm/okay, ask again — NEVER answer it on their behalf"; INVITE
goal: "NEVER say the read-back line without a time".

Not changed, for the owner to decide: the invitation and read-back do not
name the project the caller switched to (Aristo); the `NON_ANSWER_GRACE`
re-ask after a question back (a class constant, platform-wide); the
good-time fast path firing only on a plain yes.

### 78.5 Tests, deploy, rollback

`tests/test_turn_taking_cfg.py` (56 cases): pause hold per agent, the
acknowledgement vocabulary, back-channel vs answer (the 1 s margin), tail
cut, deferred override, schema/control-surface keys, the daṇḍa cut, the
"time"-in-name credit, the floor behind a question, the audit replacement.
`test_pause_tolerance.py` / `test_call1033_hearing.py` stand-ins learned
the new methods. Related suites: 395 passed; `test_copilot_control_surface`
still fails on `nishaa_ai-*` (another session's `guardrails.compliance.
dnd_phrases`), unrelated.

Applied 2026-10-12 11:40 UTC: builder → revision 10 on both twins;
`lescall-api` then `lescall-runtime` restarted, no live call. Rollback: the
five keys and the two `llm_config` lines in the builder, re-apply; the
engine changes are inert without the block (the three platform fixes stay —
they are bug fixes). Fleet rollout, once the owner confirms on a test call:
the same block in `deploy/build_pramukh_projects.py`, plus a
`max_response_length` for any agent whose scripted lines exceed 400
characters.

## 79. Vedanta test calls 33a1fbb1 / 64629614 (2026-10-12 14:19, 14:20): a date negotiation closed the call, "because" read as "why", and the model answered about the wrong project — LIVE on the test twin (revision 11)

Two calls to the owner's number, right after §78 went live; the Aristo test
(arisha@) ran concurrently, so `ws_audio_receiver.log` interleaves the
two — attribute by content, not by order.

### 79.1 Call 33a1fbb1 (33 s): ended by the callback policy
"हाँ, क्या आप मेरे को 10 मिनट बाद कॉल कर सकते हो?" → `_CALLBACK_ASK_INDIC_RE`
→ `callback_date_time='10 मिनट में'` → `_arm_callback_close` → "बिल्कुल —
हम आपको 10 मिनट में कॉल करेंगे। धन्यवाद…" → hangup. By design (a confirmed
callback is the caller's request to end the call). What the owner should
know: the platform does NOT place that call — a callback becomes a
follow-up suggestion for a person (`shared/intelligence/followup.py`); no
`scheduled_calls` row was created. Also on that call: Sarvam key
`sk_p94nh…` returned HTTP 402 (credits exhausted) and was retired; the
pool is down to 4 usable keys.

### 79.2 Call 64629614 (7.5 min): hung up on a visit-date negotiation
06:53 the agent proposed "कल मंगलवार 13 October दोपहर 2 बजे" (the caller
had asked "आप बताओ कब आना चाहिए"). 07:05 "कल की मत करो, उसके बाद का कुछ
करो" → `detect_intent` = objection (`_DEFAULT_OBJECTION_PATTERNS` has "मत
करो") → `_detect_visit_decline` correctly said "not a decline" (it saw a
time word) but the intent stayed objection → strike 1 → canned
`objection_responses[0]` ("बिल्कुल, मैं ज़्यादा टाइम नहीं लूँगा। या अगर अभी
ठीक नहीं है तो बताइए कब कॉल करूँ"). 07:14 "कल का मत करो, उसके अलावा कोई डेट
का फिक्स कर लो" → objection → strike 2 ≥ `decline_threshold` 2 → the
graceful close, whose line is `guardrails.escalation.handoff_message`
(inherited from the Ravi base agent: "मैं हमारे sales manager से इसी नंबर
पर आपको कॉल करवाता हूँ — दिन में हो तो दो घंटे के अंदर, और देर हो गई हो तो
कल सुबह सबसे पहले" = "within two hours in the day, first thing tomorrow if
it's late") → `hangup_pending` → the call ended while the caller said
"नहीं, क्या बोल रहे हो भाई तुम?". The callback regexes did NOT match either
sentence (probed); it was the two-strike close.

Fix (platform, every agent): `_is_visit_negotiation(text)` — a visit is on
the table (scheduling/confirmation stage, or the previous agent line
invited / proposed a day) and the caller moves it (`_VISIT_ALT_TIME_RE` or
`_VISIT_NEGOTIATION_CUE_RE`: "मत करो", "कोई और/दूसरा दिन", "अलावा", "उसके
बाद", "बदल", "another day"…) and it is not a call-level decline, a strong
visit refusal or a callback request → the objection intent becomes
`general`, no strike, and the per-turn `━━ VISIT DAY BEING NEGOTIATED ━━`
band (`prompt_blocks.visit_negotiation_block`) tells the model to offer two
other days and wait. The handoff line itself is the Pramukh base agent's
escalation config, unchanged — the owner decides whether a two-objection
close should promise a sales-manager callback at all.

### 79.3 "क्योंकि" is not "क्यों"
02:15 "इन्वेस्टमेंट वाली क्योंकि मैं इन्वेस्टमेंट के लिए देख रहा हूँ" —
`detect_intent`'s question-word scan is a substring test, "क्यों" sits inside
"क्योंकि" (because), the turn classified `question`, and the LLM extractor
refused `decision_factor='Investment value'` ("the caller asked a question,
they did not answer one"). The factor was asked again; the caller: "बोला ना
मैंने". The scan now blanks क्योंकि/क्योकि first. The builder also adds
"investment / इन्वेस्टमेंट / इन्वेस्टमेंट वाली / निवेश" as Investment-value
synonyms.

### 79.4 The wrong project
05:28 "…3 bhk, 4 bhk प्रमुख Vedanta में क्या-क्या मिलता है? लाइक प्रमुख
Vedanta में" (merged whole: the §78 hold worked, a 5.3 s gap) was answered
with Green County's amenities; 04:21 "साइज बता दो, कारपेट एरिया बता दो
दोनों का" — "दोनों" = the two projects the agent had just offered — was
answered with Vedanta's 3 BHK. The §55 primary band and the budget-fit
offer (Green County / Silver Stone, never confirmed — the caller's "हाँ,
बताओ" to TWO names is `ambiguous_yes`) pulled the model both ways. New
per-turn `━━ THIS TURN IS ABOUT ━━` band (`_project_focus_band` →
`prompt_blocks.project_focus_block`): the project the caller NAMED this
turn (via `pitch_switch.mentions` over the catalogue), else the project(s)
the agent's own previous line named — "'इस property', 'दोनों' mean X".
The owner's reading that "मेरे को Green County का पता होगा कार्पेट एरिया"
was a fragment of the same sentence is not what the audio shows: that
line started 24 s after "दोनों का" ended and 16 s after the Vedanta
answer finished playing (`[spoke 252.6–259.3 s]` vs `[spoke 283.3–287.9 s]`)
— a new turn, not a late fragment. The merge did everything it was asked
to on this call (every split joined, gaps up to 5.3 s).

### 79.5 Smaller defects on the same call
- 01:03/01:06 the budget question twice, 3 s apart: "…3 bhk के बारे में
  भी" was answered, "दोनों।" arrived 2.3 s later, the continuation merge
  rolled back and re-ran — but the superseded reply had "tts done" and was
  left to play, because the rollback cut only when `_tts_playing`. It now
  cuts whenever the sink's playback clock says audio is still audible
  (`_agent_audio_remaining` > 0.3 s) — the §78 lesson applied to the
  merge path.
- 03:26 "नहीं नहीं, आपने अभी मेरे को recommend किया था" rewrote
  `purchase_timeline` Within 3 months → Immediate: "नहीं" is a correction
  marker (which lets an ask-gated slot through) and "अभी" was an Immediate
  synonym. The bare "अभी"/"abhi" synonym is gone (अभी ही / अभी लेना / तुरंत
  / immediately stay).
- 01:21 the budget-fit question and Q_TIMELINE in one breath: the
  `ask_verbatim` floor now stays silent behind a reply that ends on a
  question of its own (the slot stays in the directive for the next turn).
- 02:44 the investor pitch named Vedanta although the budget (≤ ₹1 Cr)
  does not fit it and two alternatives were on offer: FIT_AND_PITCH goal —
  with a BUDGET FIT note naming other projects, ask which one, never pitch
  Vedanta.
- 00:04 the §78 tail cut fired on the pickup "हेलो नमस्ते" over the identity
  line (2.4 s left): `_tail_cut_due` now ignores greeting-only speech, the
  onset intro and the whole connect window.
- 04:04 "प्रोसीजर कैसे होता है" → "प्रोसीजर बहुत सिंपल है सर" — nothing in
  the sheet about the booking procedure; a content gap for the owner.

Tests: `tests/test_turn_taking_cfg.py` (+15: negotiation vs decline vs
callback, no strike / no hangup over two negotiations, क्योंकि, the focus
band, the floor behind a question, the tail-cut exclusions).

## 80. WhatsApp through Interakt: Pramukh's API key, the brochure as the next action after every Pramukh call (2026-10-13) — LIVE, sending ON

**Why.** Pramukh Group's WhatsApp Business number lives on **Interakt**
(interakt.ai, a Meta BSP) and they handed us its API key so the agents'
on-call promise — "I'll send the brochure on WhatsApp" — is finally kept.
§64 built everything around a tenant's OWN Meta Cloud API account (WABA id,
phone number id, system-user token, Meta webhook with a platform app secret).
Interakt has none of that: one key, templates created and approved in its
dashboard, media passed as a public URL, receipts through its own webhook.
Same tables, same stage, same sender — a second PROVIDER on the account row.

**What changed.**
- `wa_accounts.provider` (`meta` | `interakt`, migration 0031), plus
  `webhook_token` (names the tenant in the webhook URL) and
  `webhook_secret_enc` (the HMAC secret the tenant types into Interakt,
  Fernet-encrypted like the key). `save_connection` (Meta) sets `meta`;
  `save_interakt_connection` sets `interakt` and clears the Meta-only columns.
- `shared/whatsapp/interakt.py`: `verify_key` (the Get Users API filtered to
  2099 — the only side-effect-free call the public API offers; 200 = valid,
  401 = not), `send_template` (`POST /v1/public/message/`, `countryCode` +
  `phoneNumber` split by `split_phone`, `headerValues: [<media url>]`,
  `fileName`, `bodyValues` **in order of appearance in the body text** — Interakt
  is positional whether the template says `{{1}}` or `{{greeting}}`),
  `signature_ok` (`Interakt-Signature: sha256=<hex HMAC of the raw body>`),
  `parse_event` (`message_api_sent/delivered/read/failed` → `apply_status`,
  `message_received` → the same inbound/STOP path as Meta). Failures are
  `graph.GraphError`s so the sender's one retry/fail path serves both: 429/5xx
  retry, 401 marks the account `error` ("reconnect in Setup").
- **Templates are registered by hand** (`POST …/templates/register`,
  `PUT/DELETE …/templates/{id}`, `wa_templates.source='manual'`): Interakt has
  no list-templates API. The owner writes the template exactly as approved in
  Interakt (code name, language, `{{1}}`…); `templates.manual_components` turns
  it into Meta-shaped components so slots / preview / both senders work
  unchanged. A Meta sync deletes only `source='meta'` rows. A rename follows
  into playbooks and open sends.
- **Media by URL**: `wa_media.public_token` → `GET /r/f/<token>` serves that one
  file (under the existing open `/r/` prefix + nginx location — no nginx
  change). Files from before 0031 get a token on first use.
- **One brochure per project**: `wa_playbooks.media_map` (project name → media
  id) beside the default `media_id`; `stage.resolve_media` matches the send's
  project (the first project the caller agreed to, §72's
  `whatsapp_share_projects`, else the state's project, else the agent's primary
  project) case/punctuation-blind, "Vedanta" ⊂ "Pramukh Vedanta". The send row
  records `project` and `media_id`; Approvals / Activity show both.
- **A missing attachment HOLDS the send instead of failing it.** The sender
  re-resolves on every pass (`_attachment`), so a brochure uploaded after the
  call is picked up by the waiting send; the reason reads "waiting for the
  attachment for Pramukh Vedanta…". It expires with the account's TTL (48 h).
- Webhook: `POST /api/whatsapp/webhook/interakt/<token>` — `_OPEN_PREFIXES`
  gained `/api/whatsapp/webhook/interakt/` (per-tenant path, so it cannot be
  an exact match; credential = the HMAC; the prefix ends in a slash + token so
  no sibling route can start with it). 404 unknown token, 503 no secret on
  file, 401 bad signature, dedup by body hash in `wa_webhook_events`.
- UI (`SetupTab` / `TemplatesTab` / `PlaybooksTab`): provider chooser on
  connect (Interakt first), a "Delivery receipts and replies" card with the
  webhook URL + secret, "Add approved template" with the Pramukh brochure
  starter text (copy it into Interakt, same name, then save here), brochure
  per project in the playbook editor (`GET …/projects?agents=` lists the chosen
  agents' primary projects), preview shows project + attachment or the "no
  brochure yet" warning. `isConnected()` no longer needs a phone number id.

**State at deploy (2026-10-13 05:25 UTC).**
- Migration 0031 applied; `lescall-api` + `lescall-workers` restarted; SPA rebuilt.
- `WA_SEND_ENABLED=true` (was false since §64), `WA_CANARY_PHONES` empty.
- Interakt key verified live (200 on the filtered users call; a bogus key → 401)
  and connected on **angelo@, arisha@, shubhangi@** — the three owners of
  `pramukh_<project>-<dir>` agents (14 + 6 + 6; `pramukh_agent` and `pramukh`
  have no project and are excluded). Each account: default approval `auto`,
  template `pramukh_brochure` (en, DOCUMENT header, 3 variables), playbook
  "Pramukh brochure" — all that owner's project agents, recipient = the
  WhatsApp number the caller gave (else the dialled one), `{greeting}` /
  `{agent_name}` / `{project_name}` with fallbacks, approval auto, **no
  brochure files yet**.
- Consent detection needs nothing new: §72's share tracker already writes
  `whatsapp_promised` + `whatsapp_share_projects` (call 1083 has them).

**2026-10-16 — first real send through Interakt, from this VM.** Pramukh
sent the code name of the template they created from our text:
`download_brochure_o2` (Interakt's "Template id" field = the code name the
API wants). The screenshot (bucket `lescall-recordings`,
`buildreference/pramukh-interakt/pramukhinterakttemplate.jpeg`) showed a
DOCUMENT header, the three positional variables, our wording as four short
paragraphs, the STOP footer, no buttons — but not the language or the
approval state. Registered as `en` on all three accounts
(`update_template` renamed `pramukh_brochure`; playbooks followed). Pramukh
then uploaded SIX compressed brochures to `…/pramukh-interakt/brochures/`
(no Vedanta); the scratch script attached each to its project on all three
accounts with customer-facing names ("Pramukh Swarit Brochure.pdf"; Aristo
is 17.9 MB, under our 20 MB cap). Test: a queued row on angelo@'s playbook,
to the owner's number, project Pramukh Swarit → the sender delivered it in
one pass, Interakt id `b462097d-…` — which proves the key, the code name,
`en` and that Interakt accepts our `/r/f/` URL (nginx serves it with range
support, `Content-Disposition: inline`). Receipts are still not wired (no
webhook secret), so "sent" is the last state we see without a phone in hand.
The bucket route: this VM's attached SA (`lescall-recsync@…`) has object
access to `lescall-recordings` only, via ADC from the project venv — the
`gcloud` CLI's user login needs re-auth and is not used.

**What still needs a person (Pramukh's side).**
1. ~~Template~~ DONE 2026-10-16: `download_brochure_o2`, approved, `en`.
2. ~~Brochures~~ DONE for six projects 2026-10-16. **Pramukh Vedanta's PDF is
   still missing** — every Vedanta send (incl. the `pramukh_vedanta_test`
   twin) waits with "waiting for the attachment for Pramukh Vedanta" until
   it is uploaded and attached on all three accounts.
3. Optional: webhook in Interakt → Developer Settings (URL + secret from the
   Setup card) for delivered / read / STOP. Sends work without it. Interakt
   takes ONE webhook URL per account while we hold three LesCall accounts —
   route receipts by message id across owners before sharing the URL.

**Tests.** `tests/test_whatsapp_interakt.py` (21) + the 59 of §64 + the SQL
gate. Interakt is monkeypatched; the live key was probed by hand (RUNBOOK
author, read-only).

**Not done.** The Copilot prompt still says the platform cannot send WhatsApp
(`agent_copilot_api.py` ~6026); the engine still promises WhatsApp for agents
with no playbook; the call page / ContactHub chips (§64 list) are unchanged.

## 81. Blue Stone test call 1092: "I can't come" got no WhatsApp line, and "Amenities" under hi-IN was heard as "हमें देखिए" (2026-10-13) — LIVE on the Blue Stone + Aristo test twins

Call `918146432799_20261013_093544` (`pramukh_blue_stone_test-outbound` rev 1).

**1. Decline → no WhatsApp.** "अभी तो मैं नहीं आ सकती" was detected (§73) and the
declined branch jumped to CONFIRM_AND_CLOSE, whose goal opened with "say the
pre-close line"; the "if no visit was fixed, first confirm WhatsApp" clause
and the VISIT DECLINED band were both ignored. Fix: the declined branches go
to a new last stage `DECLINED_CLOSE` with **`script.entry_line`** — a new
engine hook: a stage a branch enters on this turn speaks that line verbatim
with no model turn (`_stage_entry_line`, after the stage cascade, before the
good-time fast path; rollback-safe, not committed). Skipped when the caller's
turn is a question — the model answers, its goal carries the same line. The
line ends on the pre-close question, so `_is_close_confirmation` (50% content
overlap) lets the caller's "नहीं" close the call.

**2. "What matters most" fumbled.** The caller said "Amenities" twice (re-
transcribed from `caller_audio.wav` with language auto: "Amenities" both
times); `stt_forced_language: hi-IN` gave "हमें देखिए" / "नि टी". The QA
tracker called that answered, the model invented "location और construction
quality सबसे ज़्यादा मायने रखती है", the ask_verbatim floor stacked the
question behind it; the second garble hit max_asks and the pitch began.
Fixes:
- **`languages.stt_enum_recheck`** (opt-in): `pipeline._enum_recheck` — when
  STT is locked and the router's `stt_recheck_slot()` says the turn answers
  the enum slot just asked (`last_noted_ask`, unfilled, its question in the
  last agent line), is ≤ 6 words, no "?", and names none of its values, the
  turn's audio is re-read ONCE with `language="unknown"`
  (`ENUM_RECHECK_TIMEOUT_S`, 1.5 s). The re-read replaces the text only if it
  names a value (`user_retranscribed` event, so the transcript shows it). It
  runs BEFORE the §69 whole-turn pass, which moves the piece window
  (`_retx_close_window`, extracted from `_retx_apply`'s finally).
- The ask_verbatim floor speaks the slot's **`reask_question`** on the second
  ask and treats a reply carrying either line as having asked it.
- Builders: decision_factor re-ask "माफ़ कीजिए, ठीक से सुनाई नहीं दिया —
  location, size, amenities…?", max_asks 3, Devanagari spellings of the option
  words; never-rule against stating an answer the CONVERSATION STATE lacks.

**3. Smaller, builder only:** "बस देख ही रही हूँ" is the timeline answer (Just
exploring synonyms) and no WhatsApp/cost-sheet offer in discovery; carpet vs
super built-up (Blue Stone pitch point now labels both; never-rule);
where-from gets a re-ask line and the purpose waits for it.

Tests: `tests/test_call1092_decline_factor.py` (15). Builders:
`deploy/build_pramukh_blue_stone_test.py`, `deploy/build_pramukh_aristo_test.py`.

## 82. Swarit test call 388688e3 (2026-10-15): "Vapi में ही हूँ" heard as "अभी नहीं", a budget with no figure, invented "flexible payment options", and a hang-up on "रख सकता हूँ" — figures in English BUILT (not live), four engine fixes PROPOSED

Call `918076562864_20261015_055112` (`pramukh_swarit_test-outbound` rev 1 —
the exact Akshay copy of 2026-10-14, NOT the Vedanta-approach rebuild, which
is still a proposal). Log slice: `ws_audio_receiver.log` 05:51:11–05:56:00.

**1. "अभी नहीं हूँ मैं" (turn 3).** Sarvam returned it live for a 1.56 s
segment; the recording says "Vapi में ही हूँ मैं" — re-read 20 times from
`caller_audio.wav` inside lescall-runtime (hi-IN forced and auto, start points
28.5–29.2 s, normaliser adapted on the earlier turns, static booster) every
one came back "बापी/वापी में ही हूँ मैं". The echo-mute window closed at
28.96 s and the voice starts at 29.16 s (RMS 9 → 4574), so the gate did NOT
clip the onset. A one-off provider misread that cannot be reproduced; what
made it visible was the engine's reaction: `detect_intent` → `objection` on
the universal "अभी नहीं" deferral pattern → the canned "मैं ज़्यादा टाइम नहीं
लूँगा… कब कॉल करूँ" line; the regex extractor wrote `site_visit_interest =
Maybe later` from the enum alias "अभी नहीं" with no visit on the table; the
QA tracker credited `calling_from` as answered. Corpus: 2 caller lines in
11,850 contain "अभी नहीं" and NEITHER is a deferral (this one, and "अभी
नहीं, अभी आप मुझे पूरे फीचर्स बताइए"). "बापी" is also not in
`stt_corrections.city_corrections` (only वापी / वापस).

**2. No alternatives for a low budget.** Turn 6: "मैं सोच तो रहा हूँ 4 bhk और
3 bhk का" → the LLM gap-filler wrote **`budget_range = '3BHK'`**; the money
guard (`_looks_like_amount`) accepts any digit, so a COUNT passed as an
AMOUNT. The slot was "filled", never asked, and `pitch_switch.budget_bounds
('3BHK')` is None — the whole BUDGET FIT flow (§68) was dead from turn 6. When
the caller said "बजट इससे बहुत कम है" (no figure) the model re-pitched and
re-invited (×4); the `price_too_high` guidance says "ask what range would feel
comfortable" and it skipped that step. Same class as the Vedanta
`budget='4 BHK'` tool write (memory 2026-10-13).

**3. "flexible payment options… आसान किस्तों के विकल्प".** Not in the KB. The
source is the owner-authored `price_too_high` objection guidance
(`build_pramukh_projects.py`: "…say the team can walk them through payment
options at the visit"), which the model stretched into a scheme. The site
publishes no payment terms.

**4. Hang-up on "मेरा जो बजट है ना, इससे थोड़ा ज़्यादा भी मैं रख सकता हूँ".**
`_is_close_confirm_yes` — `\b(end|cut|…|खत्म|बंद|रख|…)\b` — matched the bare
`रख` of "रख सकता हूँ" ("I can keep [it higher]"); the negation list uses `\b`
around Devanagari, so `ना` in "है ना," did not count (and `\bनहीं\b` never
matches "नहीं " at all — "नहीं रख दो" would read as a yes today). Log:
`[HANGUP] close confirmed by caller — closing` 0.9 s after the caller spoke.
Also flow: INVITE_SITE_VISIT's `max_turns: 4` moved the stage to
CONFIRM_AND_CLOSE while the caller was still objecting, and the caller's
"ठीक है, आ सकता हूँ" (a visit YES) got `[END_CALL]` + "कॉल कट कर दूँ?" with
no day fixed — the Vedanta-approach rebuild (CONFIRM_VISIT stage, ask_gated
budget before the pitch) is the structural fix.

**Figures in English — BUILT, NOT LIVE (owner request, all `Pramukh
<project>` agents on angelo@ / arisha@ / shubhangi@).** Bulbul read "₹50-55
लाख" digit by digit in Hindi, voiced the hyphen and the decimal point.
- `app/voice/spoken_numbers.py::english_numbers()` rewrites only what is
  written in DIGITS: "₹50-55 लाख" → "fifty to fifty five लाख", "₹2.15 Cr" →
  "two point one five crore", "3,890 sq ft" → "three thousand eight hundred
  ninety sq ft", "5,00,000" → "five lakh" (Indian grouping), "₹35,000" →
  "thirty five thousand rupees", 5% / 15th / 10:30 / +91 phone digits /
  years ("twenty twenty six"). Number WORDS ("दो मिनट", "डेढ़ करोड़") are
  untouched, so the language never changes. Never raises.
- Opt-in per agent: **`languages.tts_numbers_english: true`** (schema default
  False → every other agent byte-identical). Applied in `pipeline.py` on the
  text handed to TTS (`_spoken_numbers_text`, BEFORE the "–" → ", " rewrite,
  or "50–55" becomes "fifty, fifty five"); the transcript keeps the digits.
  One `[TTS:NUMBERS]` log line per changed utterance.
- `deploy/set_pramukh_tts_numbers.py --dry-run|--apply` sets the flag on the
  32 per-project agents (16 families incl. `-2`/`-3` copies and `_test`
  twins; `pramukh` / `pramukh_agent` listed, untouched); `hindi_only()` in
  `build_pramukh_projects.py` carries it so a rebuild keeps it.
- Tests: `tests/test_spoken_numbers.py` (54). To go live: `--apply` inside
  lescall-api, then `docker restart lescall-runtime` (+ lescall-api).

**Proposed (owner decides, nothing applied):** (P1) `_is_close_confirm_yes`
hang-up forms only — "रख दो/दीजिए", "फोन/कॉल रख", "बंद कर", "कॉल बंद", "बात
खत्म" — with script-aware boundaries on both lists; (P2) `_looks_like_amount`
strips `<figure> <counting unit>` (BHK, sq ft, floor, months…) before
deciding, at the merge choke point; (P3) a per-turn `━━ BUDGET NOT GIVEN ━━`
band for `budget_switch` agents (price called too high, no figure held: ask
the range in ONE question, no invite, no payment talk) + reword the
`price_too_high` guidance so nothing about payment terms is ever described;
(P4) "अभी नहीं…" counts as a deferral / visit decline only with call-or-time
words or after a time/visit question — otherwise it is the answer and goes to
the model; the deferral words join `_EVERYDAY_ALIASES`; (P5) builder STT
spellings बापी / बापि / वापि → Vapi; (P6) the Swarit test twin's flow — the
pending Vedanta-approach rebuild.

## 83. Voicemail, Live Voicemail, hold loops and the mailbox beep: a machine on an outbound line ends the call NOW (2026-10-15) — LIVE since 07:07 UTC (owner's go; `lescall-runtime` restarted, no call in progress), UNCOMMITTED

**Requirement (owner, 2026-10-15).** An outbound call that an iPhone voicemail /
Apple Live Voicemail greeting, a carrier "subscriber unavailable" announcement
or an answering machine answers must be recognised as early and reliably as
possible; the agent's speech must stop and the call must end — no sales
script, no message left. And a normal call must never be mistaken for one.

**Where §63 stood, measured on the corpus (1,189 calls, 11,850 caller lines).**

| Case | §63 behaviour | Gap |
|---|---|---|
| Carrier / iPhone voicemail greeting ("forwarded to voicemail… at the tone, please record your message") | text gate closes hard, in silence, 2.5–10 s in; 0 agent lines after it on every post-§63 call | the close DRAINED first: `hangup_pending`+`_hangup_hard` → bridge waits ≥3 s + the opening + the onset intro to finish, then hangs up — TTS billed into a mailbox |
| Hold announcement MID-call, call 917265832771 (2026-10-02, `outbound_lead_qualifier-2-outbound`, **690 s**) | `_HOLD_RE` knew the English and Hindi full sentences only | the announcement repeated every ~45 s from 2:28 to 11:06 in Gujarati, Hindi and English; the Gujarati pieces and the STT garbage between them ("i'm better off", "come back, but", "ही स्टाइल बनाईन") reached the model as the caller's turns — **17 pitches into a recording**; each matched line re-armed `MACHINE_WAIT_MAX_S`, so the wait never expired and the silence hangup never ran |
| A mailbox whose greeting STT never renders (Tamil / Marathi script is rejected by `validate_transcript`; music, then a tone) | nothing | the only evidence is the beep, and nothing read it |
| Live Voicemail / iOS 26 Call Screening | Live Voicemail reads the carrier wording (caught); Call Screening = SCREENER (caught) | none; the decision to hang up rather than wait for a late pickup is the owner's requirement |

Hold episodes in the corpus that a PERSON came back from (917042340227,
918076562864, 916378305026, 918904947733) were all under 40 s; they came back
with "hello", "okay", "ready to speak", "હેલો".

**What (platform, every agent; the beep rule is outbound-only).**

1. **`machine_line.py` widened, corpus-validated.** HOLD: "the person you are
   speaking with" (the anchor — nobody says it about themselves), the Gujarati
   announcement and its pieces (`હોલ્ડ પર રાખ`, `લાઈન પર રહો`, `વ્યક્તિ સાથે વાત
   કરી રહ્યા છો`…), Gujarati-script English (`સ્પીકિંગ વિથ`, `ઓન હોલ્ડ`).
   VOICEMAIL: STT variants ("add the tone"), Devanagari-script English
   ("ट्राइंग टू रीच", "फिनिश्ड रिकॉर्डिंग"), a FIRST-PERSON personal greeting
   ("I can't take your call… leave your name and number" — third person stays
   a person: "he can't come to the phone"), IVR menus ("press 1 for more
   options"). UNREACHABLE: `ગ્રાહક`, "not answering", "cannot be completed as
   dialed". New `is_hold_residue` / `is_residue(kind, text)`. Sweep: 47 calls
   flagged (was 43), every flagged line a machine; the corpus lines a person
   said near this vocabulary ("you have reached the wrong number", "no, not
   available right now, call me 10 minutes after", "एक मिनट होल्ड करो") stay
   people. Deliberately NOT added: "you've reached" (a person said it), "get
   back to you" / "call you back as soon as" (sales-call sentences), "try again
   later" (no attestation, human-plausible).
2. **A hold / screen is bounded as an EPISODE** (`_machine_wait_started`).
   A repeated announcement re-arms the 60 s gap timer, never the start.
   `MACHINE_HOLD_MAX_S` (90 s; per-agent `call_flow.connect.hold_max_s`) and
   `MACHINE_SCREENER_MAX_S` (120 s) end it, as does 60 s with no announcement.
   `machine_wait_expired()` reports it once; the pipeline watchdog
   (`_machine_close_tick`) and the gate (on the next repeat) close in silence
   with `call_outcome = hold_timeout` / `screener_timeout`. Before this an
   expired wait fell through to two "Sorry, I couldn't hear you" and a goodbye
   — thirty more seconds of TTS into a recording. While a hold holds the line,
   residue fragments are silent and a PERSON is recognised by
   `_looks_like_hold_pickup`: greeting, ack, question / hearing check, any
   affirmative / polite / return cue ("sorry, go ahead", "haan bolo"), or four
   or more words. The 690 s call now ends at ~2:28 + 90 s.
3. **Machine closes are IMMEDIATE.** `_arm_machine_close(kind, immediate=True)`
   sets `hangup_immediate`; the pipeline's engine loop and watchdog call
   `_abort_agent_speech` (cancel generation, flush queues, `uuid_break` via
   `sink.interrupt_playback`, `tts.stop`) and the bridge's `_maybe_agent_hangup`
   sends break + hangup at once — no 3 s minimum, no drain, no quiet check. The
   one close that SPEAKS first (`call_flow.connect.voicemail.action:
   leave_message`) passes `immediate=False` and drains as before. Kill switch
   `MACHINE_CLOSE_IMMEDIATE=false`.
4. **The mailbox beep is audio evidence** (`AudioEventClassifier._track_beep`,
   `BEEP_MIN_HOPS`=3 / `BEEP_MAX_HOPS`=12 / `BEEP_MAX_SPREAD_HZ`=40): a run of
   raw BEEP hops at ONE frequency that then STOPS. A one-hop blip, a glide (a
   whistle), a sustained tone (dial / hold tone, overruns the 1.2 s ceiling)
   and harmonic speech are not beeps (tests). `CallSession.on_beep` records it
   from the FSMN worker thread; the watchdog's `_machine_beep_close_due` ends
   the call ONLY when `machine_audio_close_allowed()` — outbound AND no PERSON
   has spoken (every caller line so far was a machine's, or there was none)
   AND ≥ `MACHINE_BEEP_MIN_SPEECH_MS` (1.5 s) of caller-side audio preceded it
   (a greeting played). Each beep is judged once. `MACHINE_BEEP_CLOSE_ENABLED`
   (telephony default on). `mark_exchange` no longer counts a machine line.
5. Not done, on purpose: FreeSWITCH `mod_avmd` stays unloaded (the in-pipeline
   detector sees the same audio and needs no dialplan change); Live Voicemail's
   late pickup is forfeited (the owner's requirement is to hang up).

**Precision safeguards, in order of strength.** (1) Text patterns anchored on
machine phrasing, every one attested in `recordings/` or vendor wording, swept
against all 11,850 caller lines. (2) Voicemail / screener wording after a
person has spoken is a person (unchanged from §63). (3) The beep closes nothing
once a person has spoken, nothing inbound, nothing without a greeting's worth
of audio first. (4) A hold is never closed before 60 s of silence or 90 s of
episode. (5) Every close logs `[MACHINE] … — hard close (immediate)` with the
line that caused it.

**Config** (`lescall-runtime` env; defaults shown):
`MACHINE_WAIT_MAX_S=60` · `MACHINE_HOLD_MAX_S=90` · `MACHINE_SCREENER_MAX_S=120`
· `MACHINE_CLOSE_IMMEDIATE=true` · `MACHINE_BEEP_CLOSE_ENABLED=true` (telephony)
· `MACHINE_BEEP_MIN_SPEECH_MS=1500` · `AEC_BEEP_MIN_HOPS=3` · `AEC_BEEP_MAX_HOPS=12`
· `AEC_BEEP_MAX_SPREAD_HZ=40`. Per agent: `call_flow.connect.hold_max_s`,
`call_flow.connect.voicemail` (§63).

**Verify.**
```bash
grep -E "\[MACHINE\]|\[HANGUP_REQUEST\] machine on the line|\[AUDIO-EVENT\] beep confirmed|\[SESSION\] beep" phase14_runtime/logs/docker/ws_audio_receiver.log
```
A voicemail call should show the machine line, `hard close (immediate)`,
`aborting agent speech`, and `[HANGUP_REQUEST] machine on the line (voicemail)`
within the same second; `calls.answer_state_reason` = `voicemail_detected`.

**Rollback.** `MACHINE_CLOSE_IMMEDIATE=false` (drain again),
`MACHINE_BEEP_CLOSE_ENABLED=false` (text only), `MACHINE_HOLD_MAX_S=100000`
(unbounded hold, §63 behaviour); the pattern widening has no flag.

**Tests.** `tests/test_machine_termination.py` (70): corpus lines both ways,
the 917265832771 replay (every fragment silent, the person's return answered,
the ceiling closing the call), the watchdog expiry, immediate vs authored-
message closes, the beep confirmer against tone / blip / glide / sustained /
speech, the pipeline tick (closes before a person, ignores after, ignores
inbound and no-greeting), the bridge's immediate hangup vs the untouched
consented drain. `test_machine_line.py` (37) unchanged and green.

**Corpus sweep** (re-run before widening any pattern): classify every
`Caller:` line in `recordings/*/transcript.txt` with `machine_line.classify`,
print every hit with its call and what the agent said after, and read the
hits — a flagged line a PERSON said is a bug, however few.
