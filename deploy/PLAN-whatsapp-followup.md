# PLAN — Post-Call WhatsApp Follow-Up (property details after the call)

Status: DRAFT — awaiting approval
Author: Claude, 2026-07-27
Goal: after a call ends, if the agent promised WhatsApp follow-up, send the
caller a WhatsApp message with details of the specific project discussed in
the call (name, location, config, price, brochure/video, visit slot), in the
caller's language.

---

## 0. Key finding: the intent layer already exists — only transport is missing

A repo sweep found zero messaging transport (no provider SDK, no credentials,
no send code), but the *entire* upstream intent layer is already built and
populated on every call:

| Existing piece | Where |
|---|---|
| Agent verbally promises WhatsApp in closing | `agents/pramukh_agent/agent.yaml:292` (en/hi/gu promise lines), ~14 agents |
| Promise recorded as a state flag | `conversation.py:1874` → `extra_flags["whatsapp_promised"]` + `whatsapp_assets` → persisted in `calls.collected_data.flags` |
| Per-agent config for the promise | `follow_up.whatsapp_promise: {enabled, assets, promise_line:{lang}}` (`schema.py::FollowUpConfig`) |
| Extractor classifies the commitment | `commitments.kind = 'whatsapp'` enum (migration 0001, `extract.py:34`) |
| Follow-up engine treats it as a forced YES | `shared/intelligence/followup.py` — `whatsapp_promised` flag forces a follow-up row |
| Automation action already declared in UI | `shared/intelligence/automation.py` `ACTIONS` → `send_whatsapp` (currently returns `not_configured`) |
| Brand-level number field | `brand.whatsapp_number` + `effective_whatsapp_number()` (`prompt_blocks.py:346`) |

So this feature = **add a transport + a send gate**, not a new pipeline.

---

## 1. WhatsApp Business Platform — what it is and the rules that shape the design

### 1.1 Provider choice
Two ways in:

- **Meta Cloud API (direct)** — free platform, you pay only per-message to
  Meta (billed in INR since Jan 2026). Full control, Graph API
  (`POST /v23.0/{phone_number_id}/messages`), webhooks for status + inbound.
  **Recommended** — we already speak httpx everywhere and don't need a BSP
  dashboard.
- **BSP (Gupshup / Interakt / AiSensy / Twilio / MSG91)** — faster onboarding,
  their UI for templates, but per-message markup + monthly fee + another
  vendor. Only worth it if Meta business verification stalls.

### 1.2 One-time setup (Meta direct)
1. Meta Business Manager → **business verification** (GST/CIN docs; days, not
   hours — start early, it also unlocks display name + higher send limits).
2. Create a **WABA** (WhatsApp Business Account) in the developer console.
3. **Register a phone number.** Constraints:
   - Must NOT be an active number on the WhatsApp consumer/Business app
     (registration hijacks it).
   - Verified via SMS **or voice OTP** — a Plivo DID works via the voice
     option (answer the verification call, read the code from the recording).
     A dedicated new DID is cleaner than reusing the main calling DID.
4. Create a **System User** → permanent access token with
   `whatsapp_business_messaging` scope. Note the `phone_number_id` and
   `waba_id`.
5. Configure **webhook** URL + verify token (see §4.4).

### 1.3 The 24-hour rule (this dictates everything)
- A business may send **free-form messages only inside a 24h customer-service
  window** opened by the *customer* messaging first.
- Our post-call message is **business-initiated** (the customer hasn't
  WhatsApp'd us) → it MUST be a **pre-approved template message**. Always.
- If the customer **replies**, a 24h window opens → we can then send anything
  (free-form text, media, location pins) without templates, free of charge.

### 1.4 Template categories & cost (India, 2026 per-message pricing)
- **Utility** (~₹0.13/msg): follow-up tied to a specific customer-initiated
  interaction/request. "Here are the details you asked for on our call, and
  your visit slot" **qualifies as utility** if worded transactionally.
- **Marketing** (~₹0.88/msg): anything promotional. Meta's classifier
  **auto-recategorizes** templates it deems promotional — avoid salesy copy
  ("limited offer!", "best price!") or we pay 7× per message.
- Utility templates sent inside an open service window are **free**.
- Templates are submitted per **language variant** (`en`, `hi`, `gu` all
  supported) and approved usually in minutes, worst case 24–48h.

### 1.5 Opt-in & opt-out (Meta policy, not optional)
- Meta requires **prior opt-in** for business-initiated messages. Opt-in
  **collected verbally during a phone call is valid** — which is exactly what
  our agents do ("Shall I send you the details on WhatsApp?"). The
  `whatsapp_promised` flag + the transcript are our consent record.
- **Send gate = consent gate**: we only message when the call produced the
  promise. No promise → no message. This keeps us clean by construction.
- Opt-out must be honored: an inbound "STOP"/"unsubscribe" must set a
  messaging DND. Gap: today DND is overloaded on `contacts.disposition =
  'rejected'` (a *calling* DND). Messaging consent needs its own home (§4.5).

### 1.6 Send limits & quality
- Unverified business: 250 unique recipients/24h → verification lifts to 1K →
  auto-scales 10K / 100K / unlimited as quality rating holds.
- Meta tracks blocks/reports per number; low quality **pauses templates**.
  Our gate (only message people who asked, minutes after they asked) is the
  best possible quality profile.

---

## 2. Choosing "the property discussed in the call" — data already on disk

Resolution order (best → fallback):

1. **`calls.collected_data.state.matched_entity`** — the session-locked
   project (`conversation.py:1790` locks the first confirmed project and
   never overrides it). Deterministic, exactly "what the call was about".
2. **`call_intelligence.product_interest`** (extract stage LLM output) —
   covers calls where the lock never fired.
3. **`call_intelligence.entities`** — last resort.

Render details from **`agents/<slug>/agent.yaml → knowledge_base.known_projects`**
(loaded in-worker via `pipeline._agent_yaml()`). Two shapes exist — normalize:
- v2: `{key: [canonical_name, location]}`
- pramukh legacy: `{"Pramukh Aura": ["Chala, Vapi", "2 & 3 BHK Apartments", "₹34–40 Lakh"]}`

Other fields, all already persisted per call:
- **Recipient**: `calls.phone` → E.164 helper needed (`shared/phone.py` keeps
  only last-10 digits; re-prefix `+91` when no country code — new
  `to_e164(phone, default_cc="91")` in `shared/phone.py`).
- **Language**: `calls.language` (`en-IN|hi-IN|gu-IN`) → template locale
  (`en|hi|gu`).
- **Visit slot**: `collected_data.state` appointment fields.
- **Name**: `contacts.name` / collected `customer_name`.
- **Assets** (brochure PDF / video): `follow_up.whatsapp_promise.assets` in
  the agent YAML; files must be at a public HTTPS URL (serve from
  agent.lescall.com or GCS signed URL ≥ template TTL) or pre-uploaded to
  Meta's media endpoint for a media `id`.

---

## 3. Message design

One **utility** template, three language variants. Header = DOCUMENT (or
VIDEO) for the brochure. Body (en variant):

```
Hi {{1}}, thanks for speaking with {{2}} today.

As discussed — *{{3}}*, {{4}}
• Configuration: {{5}}
• Price range: {{6}}
{{7}}

Reply here for directions, more photos, or to change your visit.
```

`{{7}}` = "Your site visit: Saturday 11:00 AM" or a neutral line when no
booking. Keep copy transactional (utility classification). A second, simpler
no-media template acts as fallback when an agent has no assets configured.

Sample Graph API call:

```http
POST https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages
Authorization: Bearer {SYSTEM_USER_TOKEN}

{
  "messaging_product": "whatsapp",
  "to": "919876543210",
  "type": "template",
  "template": {
    "name": "post_call_property_details",
    "language": { "code": "gu" },
    "components": [
      { "type": "header", "parameters": [
          { "type": "document", "document": {
              "link": "https://agent.lescall.com/assets/pramukh-aura.pdf",
              "filename": "Pramukh-Aura-Details.pdf" } } ] },
      { "type": "body", "parameters": [
          { "type": "text", "text": "Rakeshbhai" },
          { "type": "text", "text": "Pramukh Group" },
          { "type": "text", "text": "Pramukh Aura" },
          { "type": "text", "text": "Chala, Vapi" },
          { "type": "text", "text": "2 & 3 BHK Apartments" },
          { "type": "text", "text": "₹34–40 Lakh" },
          { "type": "text", "text": "Site visit: Sat 11:00 AM" } ] }
    ]
  }
}
```

Response carries a `wamid` message id → correlate status webhooks.

---

## 4. Architecture — where it plugs in

### 4.1 Trigger: 9th pipeline stage `whatsapp`
Append to `STAGE_ORDER` + `_STAGE_HANDLERS` in `shared/intelligence/pipeline.py`
(new `shared/intelligence/whatsapp.py::stage_whatsapp`), placed after `digest`.
Free wins: checkpointing, retry/dead-letter, `sweep_missing` re-drive.

**⚠ Backfill guard is mandatory**: `sweep_missing` re-drives *done* jobs
missing a new stage → without a gate we'd blast every historical caller on
deploy. Mirror the notify stage's freshness gate: if `ended_at` older than
`WA_FRESHNESS_MIN` (default 60 min) → mark stage `done` with
`{skipped: stale}` and send nothing.

Stage logic (decision only — no network):
```
gate:  WA_ENABLED? → flags.whatsapp_promised OR commitments.kind='whatsapp'?
       → not messaging-opted-out? → fresh? → phone valid E.164?
pick:  matched_entity → product_interest → entities
build: template name + lang + params + asset link
enqueue: shared/outbox row  kind='wa.template'  dedupe_key='wa:{call_id}'
```

### 4.2 Transport: outbox pattern
Reuse `shared/outbox.py` / `outbox_worker.py` (pending|sent|failed, attempts,
`next_attempt_at`, unique `dedupe_key` = at-most-once per call). Add a
`wa_sender` (httpx → Graph API) to the worker's sender registry. Retry on
5xx/timeouts; error codes 131047/131026 (recipient not on WhatsApp / not
opted in) are permanent-fail, recorded, no retry.

### 4.3 Wire the existing automation stub
`automation.py::_run_action` `send_whatsapp` branch → call the same builder +
outbox enqueue instead of returning `not_configured`. Rule-driven WhatsApp
(e.g. on `lead.hot`) comes free once transport exists.

### 4.4 Webhooks (dashboard API, new endpoints)
- `GET /api/wa/webhook` — Meta verify handshake (echo `hub.challenge`).
- `POST /api/wa/webhook` — two event families:
  - **statuses** (`sent|delivered|read|failed` per `wamid`) → update
    `wa_messages`, emit `contact_events` (`whatsapp.sent/.delivered/.read/.failed`)
    → shows on the ContactHub timeline for free.
  - **inbound messages** → opens the 24h window; v1: store + notify owner
    (bell/SSE) + timeline event `whatsapp.replied`; detect STOP words → set
    messaging opt-out. v2: route into copilot / a WhatsApp text agent.

### 4.5 New persistence
- `wa_messages` (migration 0010): `id, owner, call_id, contact_id, wamid,
  template, lang, to_phone, status, error, payload JSON, created_at,
  status_at`.
- Messaging consent on `contacts`: either `wa_opt_out_at TIMESTAMPTZ` column
  or `details.wa_opt_out` — separate from calling DND. Calling DND
  (`disposition='rejected'`) should ALSO block WhatsApp (superset).

### 4.6 Config & credentials
Env vars via `shared/intelligence/config.py` convention (typed readers,
kill switch re-read every tick):
```
WA_ENABLED=false            # kill switch
WA_PHONE_NUMBER_ID=...
WA_TOKEN=...                # system-user permanent token
WA_WABA_ID=...
WA_FRESHNESS_MIN=60
WA_WEBHOOK_VERIFY_TOKEN=...
WA_API_VERSION=v23.0
```
v1 = one platform-wide WABA number (all tenants send from the LesCall
number; template body carries the tenant brand name). Per-tenant numbers
later = move creds to `settings` table (needs encryption) + per-tenant
`phone_number_id`; note per-agent `brand.whatsapp_number` field already
exists as the natural surface.

### 4.7 Per-agent opt-in
New dict-shaped flag, following `clarify_commitments` precedent:
```yaml
follow_up:
  whatsapp_send:            # off by default
    enabled: true
    template: post_call_property_details
```
Missing key = False = agent never triggers sends, regardless of global env.

---

## 5. End-to-end flow

```
call ends → TelephonySession.stop() → _persist_call_row → _enqueue_intelligence
  → worker: extract → embed → followup → memory → digest → timeline
  → automation → notify → **whatsapp stage**
      gate (promise+consent+fresh+enabled) → pick project → build template
      → outbox row (dedupe wa:{call_id})
  → outbox worker → Graph API send → wamid
  → Meta webhook: delivered/read → wa_messages + contact_events timeline
  → customer replies → 24h window opens → notify owner / (v2) text agent
```

## 6. Rollout phases

- **P0 (no code)**: Meta business verification, WABA, number registration,
  submit templates ×3 languages. Longest lead time — start first.
- **P1**: `shared/phone.to_e164`, `shared/intelligence/whatsapp.py` stage +
  gate + project resolver, outbox `wa_sender`, config knobs, migration 0010.
  Canary: `WA_ENABLED=true` with allowlist env `WA_CANARY_PHONES` (own
  numbers only), verify on a test call.
- **P2**: webhook endpoints, status→timeline, STOP→opt-out, dashboard surface
  (send status chip on the call detail page).
- **P3**: wire `send_whatsapp` automation action; reply handling in copilot.
- **P4**: per-tenant WABA numbers via settings table.

## 7. Costs & risks

- ~₹0.13/call utility (only calls with a promise) — negligible vs telephony.
  If Meta recategorizes the template as marketing: ₹0.88. Watch category on
  approval.
- Recipient not on WhatsApp → permanent error, record + fall back to nothing
  (v2: SMS fallback).
- Asset URLs must be publicly fetchable by Meta; GCS signed URLs expire —
  prefer static hosting under agent.lescall.com.
- Template edits re-enter review; keep params generic so copy changes rarely.
- Multi-tenant on one number: sender identity is LesCall's, not the
  tenant's — acceptable v1, disclose in template body via brand param.
