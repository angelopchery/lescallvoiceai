import type { ReactNode } from 'react'
import { AlertTriangle, Info } from 'lucide-react'
import { Card } from './ui'
import { Table, TD, TH, THead, TR } from './kit'
import { Snippet } from './copy'

/**
 * The API reference body, rendered by /developers/reference (which is also
 * what prints).
 *
 * It takes the reader's OWN limits as props rather than documenting the
 * platform defaults. A reference that says "60 requests per minute" to a
 * customer who was granted 300 is a reference people learn to distrust.
 */

export type DocPolicy = {
  rate_per_min: number
  rate_per_day: number
  max_keys: number
  ip_restricted: boolean
}

type Props = {
  baseUrl: string
  policy?: DocPolicy
  /** Renders the anchor rail and larger headings. Off when embedded. */
  standalone?: boolean
}

const SECTIONS = [
  { id: 'start', label: 'Getting connected' },
  { id: 'auth', label: 'Authentication' },
  { id: 'limits', label: 'Access & limits' },
  { id: 'contacts', label: 'Contacts' },
  { id: 'calls', label: 'Calls' },
  { id: 'record', label: 'The call record' },
  { id: 'sync', label: 'Keeping in sync' },
  { id: 'idempotency', label: 'Safe retries' },
  { id: 'errors', label: 'Errors' },
]

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="scroll-mt-24">
      <h2 className="border-b border-[var(--border)] pb-2 font-display text-lg font-bold tracking-tight">
        {title}
      </h2>
      <div className="mt-3 space-y-3 text-sm text-soft">{children}</div>
    </section>
  )
}

function Endpoint({ verb, path, children }: { verb: string; path: string; children: ReactNode }) {
  const tone: Record<string, string> = {
    GET: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-300',
    POST: 'bg-mint-500/10 text-mint-700 dark:text-mint-300',
    PATCH: 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
    DELETE: 'bg-rose-500/10 text-rose-600 dark:text-rose-300',
  }
  return (
    <div className="break-inside-avoid rounded-xl border border-[var(--border)]">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-[var(--border)] bg-[var(--surface-2)] px-3 py-2">
        <span className={`rounded px-1.5 py-0.5 font-mono text-2xs font-bold tracking-wide ${tone[verb]}`}>
          {verb}
        </span>
        <span className="break-all font-mono text-xs font-medium">{path}</span>
      </div>
      <div className="space-y-2 px-3 py-2.5 text-sm text-soft">{children}</div>
    </div>
  )
}

function Note({ tone = 'info', children }: { tone?: 'info' | 'warn'; children: ReactNode }) {
  const Icon = tone === 'warn' ? AlertTriangle : Info
  return (
    <div className={`flex break-inside-avoid items-start gap-2 rounded-md px-3 py-2 text-sm ring-1 ${
      tone === 'warn'
        ? 'bg-amber-500/10 text-amber-700 ring-amber-500/20 dark:text-amber-300'
        : 'bg-[var(--surface-2)] text-soft ring-[var(--border)]'}`}>
      <Icon size={14} className="mt-0.5 shrink-0" aria-hidden />
      <span>{children}</span>
    </div>
  )
}

const C = ({ children }: { children: ReactNode }) => (
  <code className="rounded bg-[var(--surface-2)] px-1 py-0.5 font-mono text-[.85em] ring-1 ring-[var(--border)]">
    {children}
  </code>
)

export default function ApiDocumentation({ baseUrl, policy, standalone }: Props) {
  const base = baseUrl || 'https://agent.namaskall.com'
  const perMin = policy ? policy.rate_per_min.toLocaleString() : '60'
  const perDay = policy ? policy.rate_per_day.toLocaleString() : '20,000'

  return (
    <div className={standalone ? 'grid gap-8 lg:grid-cols-[170px_minmax(0,1fr)]' : ''}>
      {standalone && (
        <nav data-print-hide aria-label="Contents" className="hidden lg:block">
          <div className="sticky top-24 space-y-0.5">
            <div className="eyebrow mb-2">On this page</div>
            {SECTIONS.map((s) => (
              <a key={s.id} href={`#${s.id}`}
                className="block rounded px-2 py-1 text-xs text-dim hover:bg-[var(--surface-2)] hover:text-[var(--text)]">
                {s.label}
              </a>
            ))}
          </div>
        </nav>
      )}

      <div className="max-w-3xl space-y-8">
        {/* ------------------------------------------------------ start */}
        <Section id="start" title="Getting connected">
          <p>
            The Namaskall API does two things: it accepts contacts your systems push
            in, and it hands back everything the AI agent learned on every call it
            handled — the summary and scores, the full transcript, and the recording.
          </p>
          <div className="grid gap-2 sm:grid-cols-3">
            {[
              ['Base URL', `${base}/v1`],
              ['Auth', 'Bearer token'],
              ['Format', 'JSON · UTF-8 · RFC 3339'],
            ].map(([k, v]) => (
              <div key={k} className="rounded-lg bg-[var(--surface-2)] px-3 py-2">
                <div className="text-2xs font-semibold uppercase tracking-wide text-dim">{k}</div>
                <div className="mt-0.5 break-all font-mono text-xs">{v}</div>
              </div>
            ))}
          </div>
          <ol className="ml-4 list-decimal space-y-2 marker:font-semibold marker:text-dim">
            <li>
              <strong className="text-[var(--text)]">Create a key</strong> on the
              Developers page. The secret is shown once — store it in your secrets
              manager before closing the dialog.
            </li>
            <li>
              <strong className="text-[var(--text)]">Verify it</strong> with <C>GET /v1/me</C>.
              It confirms the key works, lists its permissions, reports your limits,
              and tells you which source IP we saw you from.
            </li>
            <li>
              <strong className="text-[var(--text)]">Pull your calls</strong>, then switch to
              the cursor loop in <a className="text-mint-700 hover:underline dark:text-mint-300" href="#sync">Keeping in sync</a>.
            </li>
          </ol>
          <Snippet label="Verify your key" code={`curl ${base}/v1/me \\
  -H "Authorization: Bearer lk_live_…"`} />
        </Section>

        {/* ------------------------------------------------------- auth */}
        <Section id="auth" title="Authentication">
          <p>Send the key on every request:</p>
          <Snippet code={`Authorization: Bearer lk_live_a1b2c3d4e5f60718_9xK…`} />
          <p>
            If your CRM's HTTP connector cannot set that header — a common limitation
            in packaged integrations — send <C>X-API-Key</C> instead. Both are
            equivalent. Never put the key in a query string: URLs are written to
            access logs at every hop.
          </p>
          <h3 className="pt-1 font-display text-sm font-bold">Permissions</h3>
          <p>
            A key carries only the scopes you chose when creating it. Calling an
            endpoint outside them returns <C>403 insufficient_scope</C>, naming the
            scope you need.
          </p>
          <Table>
            <THead><TH>Scope</TH><TH>Grants</TH></THead>
            <tbody>
              {[
                ['contacts:read', 'Read the contacts in your phone book.'],
                ['contacts:write', 'Create and update contacts, and file them into groups.'],
                ['calls:read', 'List calls and read their metadata and outcomes.'],
                ['transcripts:read', 'Read full call transcripts.'],
                ['intelligence:read', 'Read the AI summary, scores, commitments and action items.'],
                ['recordings:read', 'Download call recordings.'],
              ].map(([scope, grants]) => (
                <TR key={scope}>
                  <TD><span className="font-mono text-xs">{scope}</span></TD>
                  <TD>{grants}</TD>
                </TR>
              ))}
            </tbody>
          </Table>
          <Note>
            <strong>A narrow key still works on wide endpoints.</strong> A key with only{' '}
            <C>calls:read</C> can still call <C>GET /v1/calls/{'{id}'}</C> — it gets the
            call and its outcome, with out-of-scope sections dropped and listed in{' '}
            <C>omitted_sections</C>. You never have to widen a key to read the part
            you are entitled to.
          </Note>
        </Section>

        {/* ----------------------------------------------------- limits */}
        <Section id="limits" title="Access & limits">
          <p>
            Your account is allowed <strong className="text-[var(--text)]">{perMin} requests
            per minute</strong> and <strong className="text-[var(--text)]">{perDay} per day</strong>.
            That budget is shared across every key on the account — creating more
            keys does not create more throughput.
          </p>
          {policy?.ip_restricted && (
            <Note tone="warn">
              This account is restricted to specific source IP addresses (listed in the
              card above). Requests from anywhere else are refused with{' '}
              <C>403 ip_not_allowed</C>. Call <C>GET /v1/me</C> to see the address we
              observe, and contact us to add a server.
            </Note>
          )}
          <Table>
            <THead><TH>Response header</TH><TH>Meaning</TH></THead>
            <tbody>
              {[
                ['X-RateLimit-Limit', 'Requests allowed in the window closest to being exhausted.'],
                ['X-RateLimit-Remaining', 'Requests left in that window.'],
                ['X-RateLimit-Reset', 'Unix timestamp when that window resets.'],
                ['X-RateLimit-Window', 'minute or day — which window the numbers describe.'],
                ['X-RateLimit-Scope', 'key or account — which budget is binding.'],
                ['Retry-After', 'Seconds to wait. Sent only on 429.'],
              ].map(([h, meaning]) => (
                <TR key={h}>
                  <TD><span className="whitespace-nowrap font-mono text-xs">{h}</span></TD>
                  <TD>{meaning}</TD>
                </TR>
              ))}
            </tbody>
          </Table>
          <Note tone="warn">
            <strong>Back off on 429; don't retry immediately.</strong> The counter keeps
            rising while you are being refused, so a tight retry loop stays blocked for
            the whole window. Sleep for <C>Retry-After</C>, then resume.
          </Note>
        </Section>

        {/* --------------------------------------------------- contacts */}
        <Section id="contacts" title="Contacts">
          <p>
            A contact is identified by its <strong className="text-[var(--text)]">last 10
            significant digits</strong>, so <C>+919876543210</C>, <C>09876543210</C> and{' '}
            <C>9876543210</C> are one person. A 10-digit number with no country code is
            treated as Indian.
          </p>
          <Endpoint verb="POST" path="/v1/contacts">
            <p>
              Push one contact. Upserts by default, so re-sending the same person
              updates rather than duplicating. <strong className="text-[var(--text)]">Only
              the fields you send are written</strong> — an omitted field keeps its
              current value, so a partial sync can never blank out a note someone typed
              in the dashboard. <C>fields</C> merges rather than replaces.
            </p>
            <Snippet label="Push a lead" code={`curl -X POST ${base}/v1/contacts \\
  -H "Authorization: Bearer lk_live_…" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -d '{
    "phone": "+919876543210",
    "name":  "Ramesh Patel",
    "group": "Website leads",
    "fields": { "crm_id": "LD-88421", "budget": "45L" }
  }'`} />
            <p className="text-xs text-dim">
              201 when created, 200 when an existing contact was updated; the body
              carries <C>created</C> either way. Send <C>"upsert": false</C> to be told
              about the collision (409) instead.
            </p>
          </Endpoint>

          <Endpoint verb="POST" path="/v1/contacts/bulk">
            <p>
              Up to 500 contacts in one request. Always returns 200 with a{' '}
              <strong className="text-[var(--text)]">per-row result</strong> — rows that
              fail do not roll back the rows that succeeded, so a 400-row import with
              three bad numbers lands 397 leads and tells you which three to fix.
            </p>
            <Snippet label="Response" code={`{
  "submitted": 4, "created": 2, "updated": 1, "failed": 1,
  "results": [
    { "index": 0, "status": "created", "id": "735", "phone": "+919876500001" },
    { "index": 1, "status": "failed",
      "error": "'12345' is not a usable phone number" }
  ]
}`} />
          </Endpoint>

          <Endpoint verb="GET" path="/v1/contacts">
            <p>
              A page of contacts. Filter with <C>group_id</C>, <C>q</C> (name or number
              substring) or <C>phone</C> (matched on significant digits, any format).
              Paginate with <C>cursor</C> and <C>limit</C>.
            </p>
          </Endpoint>

          <Endpoint verb="PATCH" path="/v1/contacts/{id}">
            <p>
              Update in place. Sending a different <C>phone</C>{' '}
              <strong className="text-[var(--text)]">renumbers</strong> this contact — the
              row and all of its call history stay attached — rather than creating a
              second person. If that number already belongs to someone else you get
              409; merging two histories is a decision, not a side effect.
            </p>
          </Endpoint>

          <Endpoint verb="DELETE" path="/v1/contacts/{id}">
            <p>Remove a contact. Calls it took part in are preserved, not deleted.</p>
          </Endpoint>

          <Endpoint verb="GET" path="/v1/contact-groups">
            <p>
              List groups (<C>POST</C> creates one). You rarely need to create them
              explicitly — naming a new group in <C>"group"</C> on a contact push
              creates it.
            </p>
          </Endpoint>
        </Section>

        {/* ------------------------------------------------------ calls */}
        <Section id="calls" title="Calls">
          <p>
            Every call the agent handled, inbound or outbound. The <C>call_id</C> (for
            example <C>919876543210_20260812_101500</C>) is stable forever — store it as
            your foreign key.
          </p>
          <Endpoint verb="GET" path="/v1/calls">
            <p>Newest first, unless you ask for <C>order=asc</C>.</p>
            <Table>
              <THead><TH>Parameter</TH><TH>Filters to</TH></THead>
              <tbody>
                {[
                  ['since · until', 'Calls that started at or after (before) a date or RFC 3339 timestamp.'],
                  ['direction', 'inbound or outbound.'],
                  ['agent', 'One agent slug — see GET /v1/agents.'],
                  ['analyzed', 'true for calls the AI has finished processing.'],
                  ['min_lead_score', 'Calls scoring at least this, 0–100. Useful for routing only real leads.'],
                  ['sentiment', 'positive, neutral or negative.'],
                  ['phone · contact_id', 'Everything from one person.'],
                  ['cursor · limit · order', 'Pagination. limit defaults to 50, maximum 200.'],
                ].map(([p, meaning]) => (
                  <TR key={p}>
                    <TD><span className="whitespace-nowrap font-mono text-xs">{p}</span></TD>
                    <TD>{meaning}</TD>
                  </TR>
                ))}
              </tbody>
            </Table>
          </Endpoint>
          <Endpoint verb="GET" path="/v1/calls/{call_id}">
            <p>The complete record for one call — the payload to import. Shape below.</p>
          </Endpoint>
          <Endpoint verb="GET" path="/v1/calls/{call_id}/intelligence">
            <p>
              Just the AI's read: summary, scores, topics, objections, buying signals,
              entities, commitments and action items.
            </p>
          </Endpoint>
          <Endpoint verb="GET" path="/v1/calls/{call_id}/transcript">
            <p>
              Speaker-tagged turns with timestamps. Add <C>?format=text</C> for a plain
              version suitable for pasting into a note field.
            </p>
          </Endpoint>
          <Endpoint verb="GET" path="/v1/calls/{call_id}/recording">
            <p>
              Streams the call audio as a WAV, authenticated by your key. If you would
              rather store a plain link — to render an audio player in your CRM — use{' '}
              <C>recording.download_url</C> from the call record, which needs no
              credentials.
            </p>
          </Endpoint>
        </Section>

        {/* ----------------------------------------------------- record */}
        <Section id="record" title="The call record">
          <p>
            Abridged for readability; the fields shown are real. Lists such as{' '}
            <C>topics</C> and <C>buying_signals</C> each carry the supporting quote from
            the conversation.
          </p>
          <Snippet label="GET /v1/calls/{call_id}" code={`{
  "call": {
    "call_id": "919876543210_20260812_101500",
    "direction": "inbound",     "agent": "pramukh",
    "phone": "+919876543210",   "contact_id": "223",
    "language": "hi-IN",
    "started_at": "2026-08-12T10:15:00Z",
    "duration_seconds": 123.4,
    "answer_state": "answered",   // "empty" = picked up, nobody spoke
    "outcome": "visit_booked",
    "summary": "Caller asked about 3BHK availability in Surat…",
    "analyzed": true
  },
  "intelligence": {
    "customer_intent": "buy_flat",   "sentiment": "positive",
    "urgency": "high",
    "scores": { "lead_score": 82, "opportunity_score": 74,
                "risk_score": 12, "conversion_probability": 0.71 },
    "topics": [ … ], "objections": [ … ], "buying_signals": [ … ],
    "entities": { "names": [ … ], "amounts": [ … ] },
    "recommendations": { "next_actions": [ … ] }
  },
  "transcript": {
    "text": "[00:01.000]\\nAgent: Namaste…",
    "turns": [ { "at": "00:11.588", "speaker": "Caller",
                 "text": "मेरा बजट पचास लाख है।" } ]
  },
  "recording": {
    "available": true,  "mime_type": "audio/wav",
    "download_url": "${base}/api/recordings/dl/4MS_…",
    "stream_url":   "${base}/v1/calls/…/recording"
  },
  "commitments":  [ { "made_by": "agent", "kind": "visit",
                      "description": "Site visit Saturday 11am",
                      "deadline_at": "2026-08-16T05:30:00Z" } ],
  "action_items": [ … ],
  "collected_data": { "budget": "50 Lakhs", "property_type": "Apartment" }
}`} />
          <Note>
            <strong>Analysis is asynchronous.</strong> A call that ended a minute ago
            comes back with <C>"analyzed": false</C> and a null <C>intelligence</C>{' '}
            block. That is normal — come back for it shortly; it is not an error and
            does not need a retry with backoff.
          </Note>
        </Section>

        {/* ------------------------------------------------------- sync */}
        <Section id="sync" title="Keeping in sync">
          <p>
            Poll in ascending order and carry the cursor. Because paging is keyed on the
            call's own identifier rather than an offset, a call arriving halfway through
            your import cannot make you skip or re-import a row — which is exactly what
            offset paging does against a table still being written to.
          </p>
          <Snippet label="Sync loop" code={`cursor=$(cat .namaskall_cursor 2>/dev/null)

while :; do
  page=$(curl -s "${base}/v1/calls?order=asc&limit=100&cursor=$cursor" \\
              -H "Authorization: Bearer $NAMASKALL_KEY")

  # … upsert each entry of page.data into your CRM …

  cursor=$(jq -r '.next_cursor // empty' <<< "$page")
  [ -z "$cursor" ] && break          # has_more:false — you are caught up
  echo "$cursor" > .namaskall_cursor
done`} />
          <p>
            Every list response has the same envelope: <C>{'{ "object": "list", "data": [ … ], "has_more": true, "next_cursor": "4123" }'}</C>.
            When <C>has_more</C> is false, <C>next_cursor</C> is null and you are
            current — sleep, then resume from the cursor you stored.
          </p>
          <Note>
            <strong>Import calls first, enrich second.</strong> Because analysis lands
            after the call ends, a robust importer records the call immediately and
            re-fetches <C>/intelligence</C> for anything still showing{' '}
            <C>analyzed: false</C> on the next pass. Waiting for analysis before writing
            anything leaves a live call invisible in your CRM for minutes.
          </Note>
        </Section>

        {/* ------------------------------------------------ idempotency */}
        <Section id="idempotency" title="Safe retries">
          <p>
            Send an <C>Idempotency-Key</C> header — any unique string of your own — on
            every POST. If your request times out and you retry, the repeat replays the
            original response instead of writing a second contact. Replays are marked
            with <C>Idempotent-Replay: true</C>.
          </p>
          <p>
            Reuse a key with a <strong className="text-[var(--text)]">different</strong>{' '}
            body and you get <C>409 idempotency_mismatch</C> rather than a silently wrong
            answer. Generate a fresh key per logical operation, not per retry.
          </p>
        </Section>

        {/* ----------------------------------------------------- errors */}
        <Section id="errors" title="Errors">
          <p>
            Every failure returns the same envelope. Branch on <C>code</C>, which is
            stable; <C>message</C> is written for whoever reads your integration log and
            may be reworded.
          </p>
          <Snippet label="Error shape" code={`{
  "error": {
    "code": "ip_not_allowed",
    "message": "Requests from 198.51.100.7 are not permitted for this account.",
    "request_id": "req_686868d27111a79b"
  }
}`} />
          <p>Quote the <C>request_id</C> when you contact support — it pins the exact request in our logs.</p>
          <Table>
            <THead><TH>HTTP</TH><TH>Code</TH><TH>What to do</TH></THead>
            <tbody>
              {[
                ['401', 'unauthenticated', 'No key was sent. Check the header name and that your client forwards it on redirects.'],
                ['401', 'invalid_key', 'The key is malformed or unknown. Re-copy it; a truncated paste is the usual cause.'],
                ['401', 'key_revoked', 'Someone revoked it in Settings → API access. Create a new one.'],
                ['401', 'key_expired', 'The key passed its expiry date. Create a replacement.'],
                ['403', 'ip_not_allowed', 'This account only accepts approved addresses. Ask us to add yours.'],
                ['403', 'insufficient_scope', 'The key lacks a permission; details.required_scope names it.'],
                ['403', 'api_disabled', 'API access is switched off for the account. Contact support.'],
                ['402', 'plan_required', "Your plan does not include this data."],
                ['429', 'rate_limited', 'Sleep for Retry-After seconds, then resume.'],
                ['400', 'invalid_request', 'Something in the body or query is wrong; the message says what.'],
                ['409', 'conflict', 'The write clashed with an existing record — a duplicate number, usually.'],
                ['409', 'idempotency_mismatch', 'That Idempotency-Key was already used with a different body.'],
                ['404', 'not_found', 'No such record in this account. Other accounts’ records are also 404.'],
                ['500', 'internal_error', 'A fault on our side. Retry, and quote the request id if it persists.'],
                ['503', 'unavailable', 'A transient fault on our side. Retry with backoff.'],
              ].map(([status, code, action]) => (
                <TR key={code}>
                  <TD><span className="font-mono text-xs tabular-nums">{status}</span></TD>
                  <TD><span className="whitespace-nowrap font-mono text-xs">{code}</span></TD>
                  <TD>{action}</TD>
                </TR>
              ))}
            </tbody>
          </Table>
        </Section>

        <Card className="break-inside-avoid p-4">
          <div className="text-sm font-semibold">Generating a client</div>
          <p className="mt-1 text-sm text-soft">
            If your team uses an OpenAPI generator, Namaskall publishes a machine-readable
            description of everything above — ask us for it and we will send the
            endpoint. Nothing here requires it; the API is plain HTTP and JSON.
          </p>
        </Card>
      </div>
    </div>
  )
}
