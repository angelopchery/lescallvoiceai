# LesCall Multi-Tenant + BYON + Kamailio Plan (adapted to this codebase)

> **Implementation status (2026-07-11):** IMPLEMENTED on branch `saarikawithUI`.
> All code, config, tests (91 passing under calling-agent/venv, 68 under system
> python) and the seeded/imported DB (Phases 0+1) are in place. The remaining
> steps are OPERATIONAL — service restart, FS config permissions, per-DID
> dialplan cutover, Kamailio install window, OAuth credentials — and are
> sequenced with rollbacks in **`deploy/RUNBOOK.md`**. The BYON UI is at
> `/static/telephony.html`. Until the runbook steps run, the system behaves
> exactly as before (every new path falls back to `active_agent.json`).

Four deliverables, in dependency order:

1. **Multi-tenant data model + per-call agent routing** (the foundation).
2. **BYON** — a user self-registers their SIP trunk + DID and the system auto-generates
   FreeSWITCH config.
3. **Kamailio** — a SIP proxy in front of FreeSWITCH with a DB-backed carrier allow-list.
4. **Two (or N) users on the same VM**, each with their own number + their own AI agent,
   on calls simultaneously with the correct agent answering each.

The whole design rests on ONE idea: **every resource carries an `owner` (the user's email),
and an inbound call picks its agent by looking up the *dialed DID* instead of the global
`active_agent.json`.** Get that right and "two users on one VM" falls out for free — the
existing stack is already per-`conn_id`; the only true global that must die is
`calling-agent/agents/active_agent.json`.

## What exists today (starting point — do not re-derive)

- **Agents are YAML, not DB rows.** `calling-agent/agents/<slug>/agent.yaml` (+
  `routing.yaml`), loaded by `backend/app/agent_definition/registry.py` (`agent_registry`,
  hot-reload watcher started in `conversation_call_bridge.py`). The Agent Builder writes
  these files. **Keep this.** Multi-tenancy does NOT require moving personas into a DB —
  it requires an owner tag per slug and a DID→slug routing table.
- **Agent selection is global**: `conversation_call_bridge.py::_resolve_active_agent_slug()`
  reads `agents/active_agent.json` (`{"active": "<slug>"}`) — every inbound call runs that
  one agent. This is the single-tenant bottleneck to replace.
- **WS path carries `/<uuid>/<caller>` only** (`ws_audio_receiver.py::handle_connection`
  parses `parts[0]`/`parts[1]`). No DID today. `socket_server.py` does not yet read
  `Caller-Destination-Number`.
- **Outbound linkage is time-window matching**: `dashboard/server.py` logs each dial to
  `dashboard/call_link.json` before `fs_cli -x originate` (`_fs_originate_cmd`, ~line 2615);
  the bridge's `_lookup_recent_outbound_dial(caller_number)` matches it back. Fragile with
  concurrent tenants — must be supplemented by explicit channel vars.
- **Dashboard is a monolith with no auth**: `dashboard/server.py` (~2800 lines), JSON file
  stores (`outbound_contacts.json`, `call_analysis.json`, …), plus `agent_builder_api.py`
  and `agent_copilot_api.py`. No `shared/` package, no DB, no FastAPI routers.
- **FreeSWITCH config is hand-edited**: `/etc/freeswitch/sip_profiles/external/plivo.xml`,
  stock `dialplan/public/00_inbound_did.xml`. One Plivo DID `+918065953160`; INBOUND trunk
  `68928093167632317`, OUTBOUND trunk `25935838218519915` (outbound MUST use the latter);
  server egress IP `35.207.225.237` is in the Plivo trunk IP ACL.
- **Runtime topology** (keep unchanged): FreeSWITCH → ESL `socket_server.py` (:9000) →
  WS `ws_audio_receiver.py` (:9001) → `conversation_call_bridge.py` (one
  `TelephonySession` per `conn_id`) → `VoicePipeline`. Playback returns via TCP :9004.

---

## 0. Mental model — the end-to-end call flow (target state)

```
Carrier (Plivo/Twilio/…)                       ┌── user A's agent (by DID_A)
    │ SIP INVITE to <DID>@35.207.225.237:5060  │
    ▼                                          │
Kamailio  (public :5060)                       │  agent chosen purely by which
    │  • pike flood check                      │  DID was dialed → slug → owner
    │  • CARRIER_ACL: allow_source_address(1)  │
    │      (`address` table, grp 1)            │
    │  • dispatcher → FreeSWITCH backend       │
    ▼                                          │
FreeSWITCH  (private :5080, ACL=trusted_kamailio)
    │  dialplan public/ai_inbound_<DID>.xml matches destination_number
    │  → socket app 127.0.0.1:9000 async full   (same app as today)
    ▼
socket_server.py  (:9000)
    │  reads Caller-Destination-Number (DID) + Caller-Caller-ID-Number
    │  + channel vars lescall_agent_slug / lescall_campaign_id (outbound)
    │  opens media WS: /<uuid>/<caller>/<did>/<campaign>
    ▼
ws_audio_receiver.py (:9001)  decode PCMU→PCM16, 8k→16k  (unchanged)
    ▼
conversation_call_bridge.py  TelephonySession
    │  resolve_agent_slug(did=…, dial_meta=…)
    │    ──► phone_numbers: DID → inbound_agent_slug (owner = that tenant)
    │    ──► agent_registry.get(slug)   (YAML, hot-reload — unchanged)
    ▼
VoicePipeline(agent_def=<tenant's YAML agent>)  ◄── correct persona per call
```

Outbound is the mirror: the dashboard dial sets `lescall_agent_slug` /
`lescall_campaign_id` as channel variables in `_fs_originate_cmd` (and stamps `agent_slug`
into the `call_link.json` entry as fallback); the bridge resolves the agent from those
instead of the DID.

**Key property:** nothing about the process topology is per-tenant. N users share the same
Kamailio, FreeSWITCH, socket_server and ws_audio_receiver. Isolation is *data-level*
(`owner`) and *routing-level* (DID → slug). `SessionManager` already keys everything by
`conn_id`, so concurrent calls to different DIDs are just two sessions with two agent defs.

---

## 1. Multi-tenant data model (do this FIRST)

Create a new `shared/` package: `shared/db/schema.sql` + `shared/db/database.py`
(`get_db()` context manager; SQLite at `shared/db/lescall.db` with WAL mode for dev,
`{PK}` placeholder substituted per dialect so Postgres is a drop-in later — see Part B
for the full database rationale, dev-SQLite/prod-Postgres decision, and the phased
migration off the JSON file stores; `init_db()`
runs the schema plus idempotent `ALTER`s). Convention: **`owner TEXT` holds the user's
email; every list/query filters by it.**

```sql
-- users: dashboard accounts (Google OAuth). owner columns elsewhere store email.
CREATE TABLE IF NOT EXISTS users (
    id {PK}, email TEXT NOT NULL UNIQUE, name TEXT, google_sub TEXT UNIQUE,
    picture TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_login TIMESTAMP);

-- agents: an INDEX over the YAML registry, not a replacement for it.
-- slug = calling-agent/agents/<slug>/. Personas stay in agent.yaml; this row
-- exists so agents can be owner-scoped, listed per tenant, and referenced by routing.
CREATE TABLE IF NOT EXISTS agents (
    id {PK}, slug TEXT NOT NULL UNIQUE, owner TEXT, display_name TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

-- trunks: a SIP trunk/gateway (Plivo, Twilio, …). Drives FreeSWITCH gateway config +
-- the Kamailio ACL. provider_ips = comma-separated CIDRs to allow inbound.
CREATE TABLE IF NOT EXISTS trunks (
    id {PK}, name TEXT NOT NULL UNIQUE, provider TEXT DEFAULT 'plivo', owner TEXT,
    sip_domain TEXT NOT NULL, proxy TEXT, username TEXT, password TEXT,
    register INTEGER DEFAULT 0, caller_id TEXT, provider_ips TEXT,
    transport TEXT DEFAULT 'udp', config_status TEXT DEFAULT 'pending',
    last_applied_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

-- phone_numbers: a DID bound to a trunk AND an inbound agent slug. THE routing table.
CREATE TABLE IF NOT EXISTS phone_numbers (
    id {PK}, number TEXT NOT NULL UNIQUE,
    trunk_id INTEGER REFERENCES trunks(id) ON DELETE SET NULL,
    inbound_agent_slug TEXT,          -- → agents.slug → agents/<slug>/agent.yaml
    owner TEXT, direction TEXT DEFAULT 'both', label TEXT,
    config_status TEXT DEFAULT 'pending', last_applied_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

-- campaigns: outbound dialing bound to one agent slug + one gateway(trunk) + caller_id.
CREATE TABLE IF NOT EXISTS campaigns (
    id {PK}, owner TEXT, name TEXT NOT NULL, agent_slug TEXT,
    caller_id TEXT, gateway TEXT, status TEXT DEFAULT 'draft',
    max_concurrency INTEGER DEFAULT 1, retry_limit INTEGER DEFAULT 2, schedule TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

-- calls: unified inbound+outbound record; owner tags the tenant for dashboard scoping.
-- Replaces owner-blind rows in call_analysis.json for scoping purposes (the JSON can
-- stay as the analysis cache, but MUST gain an owner field too).
CREATE TABLE IF NOT EXISTS calls (
    id {PK}, conn_id TEXT UNIQUE, direction TEXT NOT NULL,
    agent_slug TEXT, campaign_id INTEGER, phone TEXT, language TEXT,
    started_at TIMESTAMP, ended_at TIMESTAMP, duration_s REAL,
    recording_path TEXT, transcript TEXT, summary TEXT, owner TEXT);
```

**Auth → owner.** `dashboard/server.py` currently has NO auth. Add Google OAuth
(session cookie) to it and a `current_owner(handler) -> str` helper that resolves the
session to an email (401 if absent). Then **every tenant-facing endpoint** — Agent Builder
list/save/activate in `agent_builder_api.py`, call history, contacts upload, outbound
dialing, and the new trunk/number endpoints — takes `owner` from the session and filters
by it (`WHERE owner=?` in SQL; `owner` field match in the JSON stores that remain). That
single pattern is the entirety of dashboard-side tenant isolation.

Also owner-tag the file stores that survive: recordings metadata, `call_link.json`
entries, `outbound_contacts.json` rows.

---

## 2. Per-call agent routing — the heart of multi-tenancy

File: `conversation_call_bridge.py`. Replace `_resolve_active_agent_slug()` with:

```python
def resolve_agent_slug(did: str = "", campaign_id=None, dial_meta: dict | None = None) -> str:
    """Resolve WHICH agent a call uses. NEVER raises — falls back to
    active_agent.json, then DEFAULT_AGENT (so CLI/run.py mode still works).

    Precedence: dial_meta['agent_slug'] (outbound channel var / call_link entry)
              > campaign.agent_slug (outbound)
              > phone_numbers.inbound_agent_slug by DID (inbound)
              > active_agent.json  (legacy global — kept as fallback only)
              > DEFAULT_AGENT
    """
    try:
        if dial_meta and dial_meta.get("agent_slug"):
            slug = dial_meta["agent_slug"]
            if (AGENTS_DIR / slug / "agent.yaml").exists():
                return slug
        from shared.db.database import get_db
        with get_db() as db:
            if campaign_id is not None:
                r = db.execute("SELECT agent_slug FROM campaigns WHERE id=?",
                               (campaign_id,)).fetchone()
                if r and r["agent_slug"]:
                    return r["agent_slug"]
            if did:
                digits = "".join(ch for ch in did if ch.isdigit())
                r = db.execute(
                    "SELECT inbound_agent_slug FROM phone_numbers "
                    "WHERE replace(replace(number,'+',''),' ','') LIKE ?",
                    (f"%{digits[-10:]}",)).fetchone()   # suffix match, CC-tolerant
                if r and r["inbound_agent_slug"]:
                    return r["inbound_agent_slug"]
    except Exception as exc:
        logger.warning("[BRIDGE] agent routing fallback: %r", exc)
    return _resolve_active_agent_slug()   # legacy path, unchanged
```

Two subtleties to preserve:

- **Suffix match on the last 10 digits** — carriers deliver a DID inconsistently
  (`+918065953160`, `08065953160`, `8065953160`). Centralize in
  `shared/phone.py::significant_digits` (last 10 digits) + `dialplan_expression`
  (`^(\+?\d*?)?<sig>$`), used by both this lookup and the dialplan generator (§3).
- **Never raises.** If `shared.db` isn't importable or nothing matches, behavior is
  today's behavior (`active_agent.json` → registry default). The live pipeline must never
  crash because the DB is absent.

`_build_agent_stack(conn_id)` then calls `resolve_agent_slug(...)` instead of the global
resolver — the rest (`agent_registry.get(slug)` → conversation/STT/TTS wiring) is
unchanged, including hot reload. Persist `owner` + `agent_slug` on the `calls` row and in
recording metadata so the dashboard scopes each call to the right tenant. Owner comes from
the `agents` index row for the resolved slug.

**How the DID reaches the bridge (the plumbing to add):**

- `socket_server.py`: read `Caller-Destination-Number` (the DID) and
  `Caller-Caller-ID-Number` from the channel data it already parses; also read
  `variable_lescall_agent_slug` / `variable_lescall_campaign_id` if present (outbound).
  Strip numbers to digits.
- Extend the media-WS URL it opens to `ws_audio_receiver` with two more positional
  segments: `/<uuid>/<caller>/<did>/<campaign>` (all optional → backward-compatible; the
  agent slug for outbound rides in `call_link.json` + channel var, not the URL).
- `ws_audio_receiver.py::handle_connection`: parse `parts[2]` (did, digits-only) and
  `parts[3]` (campaign id) exactly like `parts[1]` is parsed today, and pass them to
  `call_bridge.get_or_create(conn_id, sample_rate, caller_number=…, did=…, campaign_id=…)`.
- Outbound fallback: `_lookup_recent_outbound_dial()` already returns the dial record —
  add `agent_slug` and `campaign_id` fields to what `dashboard/server.py` writes into
  `call_link.json`, and feed that record in as `dial_meta`. The channel var is primary
  (exact, per-channel); the time-window match is the safety net.

Full inbound identity chain: **dialed DID → phone_numbers row → inbound_agent_slug →
agents/<slug>/agent.yaml (owner = tenant) → VoicePipeline persona.** No per-user ports or
processes anywhere.

---

## 3. BYON — self-service trunk + number → auto-generated FreeSWITCH config

Two new files: `shared/freeswitch_config.py` (pure renderers + `apply_config`) and a
dashboard API module `dashboard/telephony_api.py` (mounted into `server.py` the same way
`agent_builder_api.py` is). Everything owner-scoped.

### 3a. Config generators — `shared/freeswitch_config.py`

Pure functions that build XML strings (unit-testable), plus `apply_config` that writes
them under `FS_CONF_DIR` (default `/etc/freeswitch`) and reloads FS.

- `render_gateway(trunk)` → `sip_profiles/external/<trunk-name>.xml` — a `<gateway>` with
  realm/proxy/register/username/password/from-user. The existing hand-written
  `external/plivo.xml` becomes the first generated file: seed a `trunks` row for the
  current Plivo trunk (provider_ips = Plivo signaling CIDRs, owner = tech@dhronai.com)
  and confirm the generator reproduces it before deleting the hand-edited copy.
- `render_inbound_dialplan(number)` → `dialplan/public/ai_inbound_<did>.xml`:

  ```python
  def render_inbound_dialplan(number):
      from shared.phone import dialplan_expression, significant_digits
      did  = significant_digits(number["number"]) or number["number"].lstrip("+")
      expr = dialplan_expression(number["number"])   # ^(\+?\d*?)?<sig>$
      return (
        "<include>\n"
        f'  <extension name="ai_inbound_{did}">\n'
        f'    <condition field="destination_number" expression="{expr}">\n'
        '      <action application="answer"/>\n'
        '      <action application="sleep" data="500"/>\n'
        f'      <action application="socket" data="127.0.0.1:9000 async full"/>\n'
        "    </condition>\n  </extension>\n</include>\n")
  ```

  This is the same socket app the current inbound routing hits — per-DID files simply
  replace whatever single-DID rule routes calls today (audit `dialplan/public/` for the
  live Plivo rule; the stock `00_inbound_did.xml` is inert example config).
- `render_acl(cidrs)` → `autoload_configs/acl.conf.xml`. **Critical caveat:** once §4
  lands, the FS external profile enforces `apply-inbound-acl=trusted_kamailio`, so this
  file MUST always re-emit the `trusted_kamailio` list (127.0.0.1 + Kamailio's IP). If an
  "apply" regenerates the ACL and forgets it, all inbound breaks. The carrier allow-list
  itself lives in Kamailio (§4), not here.
- `apply_config(trunk, numbers, reload=True, all_provider_cidrs=…)` writes the gateway
  XML, one dialplan file per number, optionally regenerates `acl.conf.xml`, then reloads
  via `fs_cli -x` (`reloadxml`, `sofia profile external rescan`, `reloadacl`). The
  dashboard already shells `fs_cli` for originate, so no ESL fallback is needed — but the
  dashboard process must have write access to `FS_CONF_DIR` (a `lescall-fsconfig` group on
  the dirs, or a small sudoers-scoped helper script; decide once, document in the module).

`FS_CONF_DIR`, `FS_CLI`, `AI_SOCKET`, `KAMAILIO_IP` all env-overridable.

### 3b. Self-service API — `dashboard/telephony_api.py`

Endpoints (session-authed, `owner` from `current_owner`):

- `POST /api/trunks` — user registers their trunk. Namespace the FS gateway name:
  `name = f"{_slug(owner)}_{name}"` — gateway names are global in FreeSWITCH; the owner
  prefix prevents two tenants' trunks colliding. Then
  `kamailio_acl.sync_carrier_acl(_all_provider_cidrs(db))` to open the carrier at the edge.
- `POST /api/trunks/{id}/apply` — 404 unless owned by caller; loads the owner's
  `phone_numbers` for the trunk; `fsc.apply_config(...)`; re-sync Kamailio ACL; update
  `config_status`/`last_applied_at`.
- `POST /api/numbers` — register a DID, bind to `trunk_id` (ownership-checked) and
  `inbound_agent_slug` (must be an agent slug owned by the caller — validate against the
  `agents` index). **This binding is what closes the loop for §2.**
- `GET /api/telephony/server-info` and `GET /api/trunks/{id}/instructions` — return the
  SIP URI (`<DID>@35.207.225.237:5060`), public IP, RTP range and firewall notes so the
  user knows what to configure on their carrier. Provider-agnostic (Plivo/Twilio/Telnyx
  all work once their signaling IPs are in `provider_ips`). For Plivo specifically, the
  instructions should note the inbound-vs-outbound trunk split (origination URI on the
  inbound trunk; outbound must use the outbound trunk's credentials) and that the server
  egress IP must be in the Plivo trunk IP ACL.

`_all_provider_cidrs(db)` unions `provider_ips` across **all** trunks because the Kamailio
ACL is one global list (grp 1). Adding a trunk widens the allow-list; it never replaces
other tenants' entries.

---

## 4. Kamailio — SIP proxy in front of FreeSWITCH

Why: (a) a single public SIP edge with flood protection + a **DB-backed carrier
allow-list** updatable at runtime without touching FreeSWITCH; (b) room to add a second
FreeSWITCH node later via dispatcher. Media (RTP) flows **direct** carrier↔FreeSWITCH;
Kamailio is signaling-only (no rtpengine needed on this single non-NAT host). No carrier
reconfiguration needed: Plivo already points at `35.207.225.237:5060`; Kamailio takes over
:5060 and FreeSWITCH's external profile moves to private :5080.

### 4a. `kamailio/kamailio.cfg` (essentials)

```
listen=udp:<PRIVATE_IP>:5060 advertise 35.207.225.237:5060

loadmodule "db_sqlite.so"   loadmodule "permissions.so"
loadmodule "dispatcher.so"  loadmodule "pike.so"  ...

modparam("dispatcher","list_file","/etc/kamailio/dispatcher.list") # setid 1 = FS backends
modparam("dispatcher","flags",2)                                   # OPTIONS keepalive
modparam("permissions","db_url","sqlite:///etc/kamailio/kamailio.db")
modparam("permissions","db_mode",1)                                # read `address` table

request_route {
    mf_process_maxfwd_header("10"); pike_check_req(); sanity_check(...);
    if (has_totag()) { loose_route(); t_relay(); exit; }
    if (is_method("INVITE|REGISTER|SUBSCRIBE|MESSAGE")) route(CARRIER_ACL);
    record_route();
    route(TO_FREESWITCH);
}
route[TO_FREESWITCH] {
    if (!ds_select_dst("1","4")) { send_reply("503","Service Unavailable"); exit; }
    t_on_failure("RELAY_FAILOVER"); t_relay();
}
route[CARRIER_ACL] {
    if ($si=="127.0.0.1" || $si=="<PRIVATE_IP>" || $si=="35.207.225.237") return;
    if (allow_source_address("1")) return;              # trusted carrier (grp 1)
    xlog("L_WARN","REJECT untrusted $si"); sl_send_reply("403","Forbidden"); exit;
}
```

(SQLite via `kamailio-sqlite-modules` keeps this VM Postgres-free; the `db_url` is the
only line that changes if/when Postgres arrives.)

**Kamailio does NOT do DID routing.** It verifies the source IP and forwards everything to
FreeSWITCH; FreeSWITCH's `public` dialplan does the DID→agent match (§3a). Keep that
separation — adding a number never touches Kamailio, only FreeSWITCH.

### 4b. The carrier allow-list — the `address` table

Seed `/etc/kamailio/kamailio.db` with Kamailio's standard `version`, `address(grp,
ip_addr, mask, port, tag)`, `trusted` tables (`kamdbctl` or a checked-in
`kamailio/schema-sqlite.sql`).

**Runtime updates without restart** — new `shared/kamailio_acl.py`:

```python
def sync_carrier_acl(cidrs: list[str]) -> dict:
    # 1. rewrite grp-1 of the address table from the given CIDRs (sqlite3 stdlib)
    with sqlite3.connect(KAMAILIO_DB_PATH) as conn:
        conn.execute("DELETE FROM address WHERE grp=?", (ACL_GROUP,))
        for ip, mask in parsed:
            conn.execute("INSERT INTO address (grp,ip_addr,mask,port,tag) "
                         "VALUES (?,?,?,0,'carrier')", (ACL_GROUP, ip, mask))
    # 2. live reload
    subprocess.run(["kamcmd", "permissions.addressReload"], timeout=5, check=False)
    return {"synced": len(parsed)}
```

The dashboard runs directly on this host (`dashboard/start.sh`, no container, no systemd),
so it can call `kamcmd` directly — put the dashboard user in a group with rw on Kamailio's
ctl socket (`modparam jsonrpcs/ctl` socket mode/group). If that permission ever becomes a
problem, fall back to the touch-a-trigger-file + systemd `.path` unit pattern
(`kamailio/systemd/lescall-kamailio-reload.{path,service}` running
`kamcmd permissions.addressReload`).

`sync_carrier_acl` **never raises into the caller** — a trunk "apply" still succeeds if
Kamailio is momentarily down; the ACL reconciles on the next apply or at boot (§4c).

### 4c. Boot-time self-heal (learned the hard way)

If the `address` table is ever empty (fresh DB, bad migration), **every** carrier INVITE
gets silently 403'd and inbound looks "dead" — the same failure smell as the June 1
FreeSWITCH OOM incident, so make it impossible:

- `deploy/lescall-kamailio-acl-seed.sh` — dependency-free; rebuilds grp-1 from
  `SELECT provider_ips FROM trunks` in `shared/db/lescall.db` (splitting the comma list),
  then retries `kamcmd permissions.addressReload` until the ctl socket is up.
- `deploy/lescall-kamailio-acl-seed.service` — runs it at boot, after `kamailio.service`.

Two write paths to the ACL, same `address` table: live (dashboard apply →
`sync_carrier_acl`) and boot (`acl-seed.sh`).

### 4d. FreeSWITCH side of the handshake

- External sofia profile: set `sip-port` to 5080 on the private IP (it's `$${external_sip_port}`
  in `vars.xml` today) and `apply-inbound-acl=trusted_kamailio` (loopback + Kamailio's IP
  only). Direct carrier→FS INVITEs are rejected; everything comes via the proxy.
- `dispatcher.list` (setid 1): one line today — `sip:<PRIVATE_IP>:5080`. Add a node +
  `kamcmd dispatcher.reload` to scale later.
- Update `phase14_runtime/check_runtime.sh` to also health-check Kamailio (process +
  `kamcmd dispatcher.list` shows the FS backend up).

---

## 5. Two users on the same VM, calling simultaneously — why it just works

Put §1–§4 together and the requirement is **emergent**, not a separate feature:

- User A registers trunk A (provider IPs → global ACL) + DID_A, bound to **agent slug A**
  (owner = A). User B does the same with DID_B → **slug B** (owner = B). Both point their
  carrier origination at the same `35.207.225.237:5060`.
- Carrier sends INVITEs for DID_A and DID_B — possibly at the same instant. Kamailio
  ACL-checks the source IPs (both carriers in grp 1) and forwards both to FreeSWITCH.
- FreeSWITCH matches `ai_inbound_<DID_A>` vs `ai_inbound_<DID_B>` → both hit
  `socket 127.0.0.1:9000 async full` as **two independent channels** with different
  `Caller-Destination-Number`.
- `socket_server` + `ws_audio_receiver` already handle each as a distinct `conn_id`. Each
  `TelephonySession` calls `resolve_agent_slug(did=DID_A)` → slug A, `(did=DID_B)` →
  slug B. Two `VoicePipeline`s run concurrently with different YAML personas; each call's
  recording + `calls` row is tagged with the right `owner`.

No per-user process, port, or config file beyond the DID→slug binding row. Scaling to N
tenants = N rows in `phone_numbers` + N agent folders, and their carrier IPs in
`provider_ips`. Concurrency is bounded by CPU/Sarvam credits, not by design.

Shared choke points to audit for per-tenant leakage (all process-global today):

- `agents/active_agent.json` — becomes fallback-only; nothing multi-tenant may read it.
- `call_link.json` time-window matching — caller-number keyed; two tenants dialing the
  same customer concurrently could cross-match. The `lescall_agent_slug` channel var (§2)
  is the primary key precisely to close this.
- The Groq/LLM daily quota and the Sarvam key pool (`dashboard/api_key_pool.json`) are
  shared across tenants — fine for now, but per-tenant metering belongs on the roadmap.
- Any module-level state in `pipeline.py`/`conversation.py` must key by call, never by
  process. (Existing `SessionManager` and per-session `CallState` already comply.)

---

## 6. Build order (dependency-correct)

1. **DB**: create `shared/db/{schema.sql,database.py,seed.py}` per §1 (SQLite + WAL).
   Seed: the existing Plivo trunk, DID `+918065953160` → whatever `active_agent.json`
   currently points at, owner `tech@dhronai.com`; one `agents` index row per existing
   YAML slug.
2. **Auth → owner**: Google OAuth session in `dashboard/server.py` + `current_owner`.
   Owner-scope ONE surface first (Agent Builder list/save/activate) to prove the boundary.
3. **`shared/phone.py`**: `significant_digits` + `dialplan_expression` (used by both
   routing and config gen — build before 4 and 6).
4. **`resolve_agent_slug`** in `conversation_call_bridge.py` (§2). Unit-test: DID suffix
   match for E.164/national/truncated forms; fallback chain with no DB present.
5. **DID plumbing**: `socket_server.py` extracts `Caller-Destination-Number` +
   `variable_lescall_{agent_slug,campaign_id}`; WS path grows `/<did>/<campaign>`;
   `ws_audio_receiver.py` parses and forwards; `call_bridge.get_or_create(...)` signature
   extended. Add `agent_slug` to `call_link.json` entries in `dashboard/server.py`.
6. **BYON config gen**: `shared/freeswitch_config.py` (§3a). Verify it reproduces the
   current hand-written `plivo.xml` + inbound routing before switching over.
7. **BYON API**: `dashboard/telephony_api.py` (§3b), owner-scoped, gateway names
   owner-prefixed. Add Numbers/Trunks pages to the dashboard UI.
8. **Kamailio**: package install (+ `kamailio-sqlite-modules`), `kamailio/kamailio.cfg`,
   sqlite schema, `shared/kamailio_acl.py::sync_carrier_acl`; call it from
   `create_trunk`/`apply_trunk`. Move FS external profile to private :5080 with
   `apply-inbound-acl=trusted_kamailio`. **Do this in one maintenance window** — between
   the port move and Kamailio being up, inbound is down.
9. **Resilience**: `deploy/lescall-kamailio-acl-seed.{sh,service}`; extend
   `check_runtime.sh`.
10. **Outbound (mirror)**: campaigns table drives dialing; `_fs_originate_cmd` gains
    `lescall_agent_slug=…,lescall_campaign_id=…` channel vars and originates through the
    campaign's gateway (**must remain the Plivo OUTBOUND trunk for the existing number —
    outbound via the inbound trunk gets 403/4180**); bridge resolves agent from
    campaign/channel var.

### Verification checklist

- Register 2 trunks + 2 DIDs under 2 different owners, each bound to a different agent slug.
- `phone_numbers` suffix lookup returns the right slug for E.164, national, and truncated
  forms of each DID; with the DB file removed, calls still run the `active_agent.json`
  agent (legacy fallback intact).
- `apply` writes `sip_profiles/external/<owner>_<trunk>.xml` +
  `dialplan/public/ai_inbound_<did>.xml`, reloads FS, and `acl.conf.xml` still contains
  `trusted_kamailio`.
- `sync_carrier_acl` populated `address` grp 1 and the reload took
  (`kamcmd permissions.dumpAddresses`).
- Place two concurrent inbound calls to the two DIDs: two `TelephonySession`s log two
  different `Active agent:` slugs; two `calls` rows land with two different owners; each
  recording's metadata carries the right owner.
- Outbound regression: a dashboard dial still greets with `{contact_name}`, uses the
  outbound Plivo trunk, and the bridge resolves the agent from the channel var (kill the
  `call_link.json` fallback in a test to prove it).

---

## 7. Files to create/change (quick index)

| Concern | File | New/Change |
|---|---|---|
| Data model | `shared/db/schema.sql`, `shared/db/database.py`, `shared/db/seed.py` | new |
| Tenant boundary (API) | `dashboard/server.py` (OAuth + `current_owner`), `dashboard/agent_builder_api.py` (owner-scope) | change |
| Per-call agent routing | `conversation_call_bridge.py` (`resolve_agent_slug`, `dial_meta`) | change |
| DID plumbing | `socket_server.py`, `ws_audio_receiver.py` (WS path segments 3–4) | change |
| DID normalization | `shared/phone.py` | new |
| BYON config gen | `shared/freeswitch_config.py` | new |
| BYON API | `dashboard/telephony_api.py` (+ dashboard UI pages) | new |
| Kamailio | `kamailio/kamailio.cfg`, `kamailio/schema-sqlite.sql`, `shared/kamailio_acl.py`, `dispatcher.list` | new |
| Kamailio boot/reload | `deploy/lescall-kamailio-acl-seed.{sh,service}` (+ optional reload `.path`/`.service`) | new |
| FS hardening | external profile → :5080 + `apply-inbound-acl=trusted_kamailio`; retire hand-edited `plivo.xml` | change |
| Outbound | `dashboard/server.py::_fs_originate_cmd` (channel vars, campaign gateway), `call_link.json` schema (+`agent_slug`) | change |
| Health checks | `phase14_runtime/check_runtime.sh` (Kamailio) | change |

Agents themselves (`calling-agent/agents/<slug>/agent.yaml`, `agent_registry`, hot reload,
Agent Builder write path) are deliberately **unchanged** — multi-tenancy wraps around them.

---

# Part B — Establishing the database (plain-language briefing · built to be re-read)

Everything above assumes a `shared/db/` database exists. This part is the standalone case
for it and the migration plan — written so it can be re-read without a database background.
Every technical word gets a plain meaning before it's used.

## B1. What the system stores things in today

Right now, everything the dashboard remembers lives in files — a kind of text file called
JSON. Think of a JSON file as one long notebook. There are about a dozen of them in
`dashboard/`:

- `outbound_contacts.json` — the notebook of people to call (~6 KB today)
- `call_analysis.json` — the notebook of call summaries (~9 KB today, grows with every call)
- `call_link.json` — which number was dialled and when (~14 KB)
- `follow_ups.json`, `follow_up_automation.json`, `manual_followups.json` — follow-up notebooks
- `api_key_pool.json`, `sarvam_*.json` — key-rotation state
- plus `calling-agent/agents/active_agent.json` and `phase14_runtime/live_calls_state.json`

To the computer, each is a single document read first line to last. Hold onto one trait,
because everything later depends on it:

> **The key thing about a notebook:** to change one line, the program rewrites the entire
> notebook from scratch. Not "edit line 5" — more like "recopy the whole book by hand,
> with line 5 changed." Today the notebooks are small, so nobody notices.

## B2. What a database actually is

A database is two things at once: your data, kept in an organised shape, and a program
that reads and writes that data for you — safely, quickly, and for many things happening
at the same time. Like a spreadsheet on steroids run by a tireless librarian: you don't
flip through pages yourself — you ask, and it fetches exactly what you asked for.

The organised shape has three words:

| word | plain meaning | in this system |
|---|---|---|
| **table** | one sheet, like an Excel tab | one table for contacts, one for calls, one for trunks… |
| **row** | one line = one thing | one row in `contacts` = one person; one row in `calls` = one call |
| **column** | one field | phone, name, budget, language, owner |

"The contacts table" is a sheet where every row is a person and the columns are their
details. A database makes that official and hands it a manager.

## B3. Every term used in this plan, explained

- **primary key** — a unique ID for each row, like an Aadhaar number. Every call already
  has one (`conn_id`), but today a call gets identified three different ways: that code,
  a recordings folder name, and a phone number. A primary key means one official ID, so
  nothing gets mismatched.
- **foreign key** — a link from one table to another, like writing the customer's ID on
  an order slip. Today calls are matched to contacts by comparing phone-number text
  (fuzzy); a foreign key makes the link exact. `phone_numbers.trunk_id → trunks.id` in §1
  is one.
- **index** — a sorted shortcut, like the index at the back of a book. When a call comes
  in, "who is this number?" becomes an instant jump instead of scanning the whole contact
  file. The DID→agent lookup in §2 needs exactly this.
- **query / SQL** — asking the database a precise question in its language: "every warm
  lead in Surat we haven't called this week." Instead of code looping through files by
  hand, one question and the database does the work — "who to dial next", "who's due for
  follow-up", "which leads are hot".
- **transaction** — changes that all happen or none do, like a bank transfer. "Mark the
  contact called + add to their history + log the attempt" commit together; a crash can't
  leave it half-finished.
- **concurrency** — many things at once, like several checkout counters open. A campaign
  dialling several numbers while inbound calls also arrive is concurrency — exactly the
  multi-tenant situation Parts §1–§5 build toward.
- **race condition / lost update** — two clerks each photocopy the contact book, each
  edits their copy, each saves; whoever saves last wins and the other's change silently
  vanishes. With JSON notebooks, any two processes (dashboard + bridge, or two concurrent
  dials) writing the same file at the same moment can do this. A database prevents the
  whole class by design.
- **atomic write** — a save fully lands or not at all, like a door never stuck halfway.
  Today, if the machine dies mid-save, a notebook can be left half-written — corrupting
  the whole file. This machine has run out of memory and disk before (the June 1
  FreeSWITCH OOM), so it's not hypothetical.
- **durability** — once saved, it survives a crash. Ink dried on the page.
- **idempotency** — doing it twice = doing it once, like a well-made Submit button.
  Critical for the client-ERP hand-off: calling the same lead five times must not push
  five duplicate leads into the client's software.
- **outbox** — a reliable "to-send" tray. Drop a letter in; a worker delivers it, marks
  it sent, retries if the destination is down. Nothing lost, nothing sent twice. This is
  how qualified leads reach a client's ERP (e.g. Pramukh) safely.

## B4. Why loose files start to hurt at this volume

At one call at a time, files are fine — that's why they've worked so far. The trouble
arrives with campaigns dialling many numbers while inbound calls arrive too — and it
doubles the moment there are two tenants (§5). Four cracks open:

1. **Rewriting the whole notebook, every time.** Every finished call rewrites all of
   `call_analysis.json`. Small today, but it grows with every call and never shrinks —
   each save gets slower as history accumulates. It snowballs.
2. **Two dials at once step on each other.** The two-clerks problem, live: several
   concurrent dials updating `outbound_contacts.json` means one update can silently
   overwrite another. With two tenants sharing the files, a tenant could clobber
   another's data.
3. **A crash mid-save corrupts a whole file.** Because a save rewrites the entire
   notebook, a badly-timed crash leaves it half-written and unreadable — losing
   everything in it.
4. **Finding one contact means reading all of them.** Every incoming call scans the whole
   contact file to recognise the number — during exactly the busy moments when the delay
   costs most. And the multi-tenant routing in §2 (DID → agent) *requires* an instant
   indexed lookup.

None of these is an emergency today. All four become daily once phones are busy in both
directions — and item 4 becomes blocking the day the first second tenant signs up. That's
why this lands now, before the volume arrives.

## B5. What the database fixes, in this system's terms

- **Recognise a caller instantly** — an index on the phone number turns "who is this?"
  into an instant lookup, so an inbound call already knows the lead, their history, and
  never re-asks what an earlier call learned.
- **Dial many, lose nothing** — transactions let several dials update several contacts at
  the same moment without clobbering each other.
- **Survive a bad moment** — atomic + durable writes mean a crash never corrupts data.
- **Ask real questions** — "who do we dial next, respecting cool-down and call caps?",
  "who's due for follow-up now?", "show every hot lead" — one query each, instant.
- **Hand the client's ERP each lead exactly once** — outbox + idempotency: send once,
  retry automatically if their system is down, treat a "duplicate" reply as already done.
- **Tenant isolation** (the Part A payoff) — `WHERE owner=?` on real tables is a hard
  boundary; per-tenant filtering of shared JSON files is not.

## B6. Which database — the two kinds worth knowing

- **SQLite** — the whole database in a single file. No separate program to run, zero
  setup, with all the safety of a real database. Its one catch: **one writer at a time.**
  Fine when calls happen one after another; awkward when many dials write at once.
- **PostgreSQL** — a proper database service running in the background, with a "front
  desk" that lets many workers read and write simultaneously. A little more to set up
  and keep running.

**Recommendation:** because this system is heading into many-calls-at-once *and*
multiple tenants on one VM, build for **Postgres in production, SQLite for dev/tests**.
This is exactly why §1's `database.py` uses the `{PK}` dialect placeholder: the same
`schema.sql` and `get_db()` work under both, and moving prod to Postgres is a connection
string change, not a rewrite. (SQLAlchemy — a translator layer so code isn't glued to one
database brand — is the heavier alternative; the thin `get_db()` + dialect substitution
achieves the same portability with less machinery, so prefer it unless the ORM is wanted
for other reasons.)

One dependency note: §4's Kamailio ACL keeps its **own** small SQLite file
(`/etc/kamailio/kamailio.db`) either way — it's Kamailio's private table, deliberately
not coupled to the app database.

## B7. The migration plan, and why it's safe

> **The golden rule:** never demolish the old bridge while cars are on it. Build the new
> one alongside, move one lane at a time, keep the old bridge until the new one is proven
> ("strangler-fig" migration). Every phase keeps the old files as fallback and has an
> on/off switch (env flag), so anything can be undone in seconds.

- **Phase 0 — Stand up the empty database.** `shared/db/{schema.sql,database.py,seed.py}`
  (= build-order step 1 in §6): create the Part A tables plus `contacts`,
  `call_summaries`, `follow_ups`, and `outbox`. Nothing reads from it yet; the system
  runs exactly as before. *Safe: nothing depends on it.*
- **Phase 1 — One-time import + count check.** Import `outbound_contacts.json`,
  `call_analysis.json`, `call_link.json`, and the follow-up files into their tables
  (owner = tech@dhronai.com for all existing rows); confirm row counts match the files.
  Files remain the source of truth. *Safe: a rehearsal with a safety net.*
- **Phase 2 — Build the client-ERP outbox directly in the database.** The lead hand-off
  (e.g. to Pramukh's software) ships on brand-new ground: qualified lead → `outbox` row →
  a small worker delivers it, records sent / duplicate / retry-later. Nothing old to
  break, and it proves the database in real use on the lowest-risk surface. *Safe: a new
  feature, no regression surface.*
- **Phase 3 — Move contact & campaign state into the database.** The real cure for
  "two dials clobber each other" and the foundation for the §6-step-10 campaign dialer
  and per-tenant contact lists. Dual-write (file + DB) behind a flag, compare side by
  side, then flip reads to the DB. *Safe: dual-write + instant flip back to files.*
- **Phase 4 — Move call summaries over.** Ends "rewrite the whole analysis file every
  call"; the `calls` table (§1) becomes the owner-scoped record the dashboard queries;
  `call_analysis.json` stays as a read-only export until retired. *Safe: per-store flag.*
- **Phase 5 — Retire the old files; add housekeeping.** Stop writing each notebook once
  proven; keep automatic nightly backups/exports of the DB; add a clean-up job for old
  `recordings/` (the disk has hit 95% before). *Safe: backups + exports are the escape
  hatch.*

**Order of attack: Phases 0 → 2 → 3 first.** Phase 0 is a prerequisite for all of Part A
anyway; Phase 2 delivers the reliable client hand-off; Phase 3 fixes the collision risk
that concurrency makes daily. Phases 1, 4, 5 are tidy-up with no deadline. Note the
routing tables in Part A (`trunks`, `phone_numbers`, `agents` index, `campaigns`) are
DB-native from day one — they never exist as JSON, so they need no migration.

## B8. One-glance glossary

| term | in one line |
|---|---|
| database | Your data in an organised shape, plus a program that reads & writes it safely for many things at once |
| table | One sheet (like an Excel tab) — contacts, calls, trunks |
| row / column | A row is one thing (one contact); a column is one detail (phone, budget, owner) |
| primary key | A unique ID for each row — the row's Aadhaar number |
| foreign key | A link from one table to another — the customer ID on an order slip |
| index | A sorted shortcut so you find a row without reading them all |
| query / SQL | Asking the database a precise question in its language |
| transaction | Changes that all happen or none do — like a bank transfer |
| concurrency | Many things happening at the same time |
| race / lost update | Two changes collide and one silently overwrites the other |
| atomic write | A save that fully lands or not at all — never half-written |
| durability | Once saved, it survives a crash or power cut |
| idempotency | Doing something twice has the same effect as doing it once |
| outbox | A reliable "to-send" tray that delivers once and retries failures |
| SQLite | A whole database in a single file; simple, but one writer at a time |
| PostgreSQL | A database service that handles many readers & writers at once |
| strangler-fig | Replacing an old system piece by piece, never all at once |

**The whole thing in two sentences:** the data has outgrown loose notebooks, and a
database is simply the filing system — plus a manager — that keeps it fast, safe, and
answerable when calls fly in both directions for more than one tenant at once. Nothing
above needs memorising; when a word comes up, this page has the plain version.
