import { Card, Button, IconButton, cx } from '../../components/ui'
import { Field, Input, Textarea, Select } from '../../components/kit'
import type { Routing, WizardData } from '../../api/agents'
import { CalendarDays, ChevronDown, ChevronUp, PhoneIncoming, PhoneOutgoing, Plus, Trash2 } from 'lucide-react'

/* ──────────────────────────────────────────────────────────────────────────
 * Shared agent form sections.
 *
 * Each section renders one slice of the WizardData shape and reports edits
 * through `set(patch)`. Used by BOTH the Create wizard (one section per step)
 * and the Edit → Settings tab (all sections stacked), so the two pages always
 * expose the same structured fields.
 * ────────────────────────────────────────────────────────────────────────── */

export const LANGS = [
  { code: 'hi', label: 'Hindi' },
  { code: 'en', label: 'English' },
  { code: 'gu', label: 'Gujarati' },
  { code: 'mr', label: 'Marathi' },
]

export const SLUG_RE = /^[a-z0-9_-]+$/i

export type SectionProps = {
  d: WizardData
  set: (patch: Partial<WizardData>) => void
}

/* ───────────────────────── Basics ───────────────────────── */
export function BasicsSection({ d, set, showSlug = true, showErrors = false }: SectionProps & { showSlug?: boolean; showErrors?: boolean }) {
  const slugOk = SLUG_RE.test(d.name)
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {showSlug && (
        <Field label="Slug (folder name) *">
          <Input
            value={d.name}
            placeholder="acme_realty"
            aria-invalid={showErrors && (!d.name || !slugOk) ? true : undefined}
            className={cx(showErrors && (!d.name || !slugOk) && 'ring-rose-400/50')}
            onChange={(e) => set({ name: e.target.value.trim() })}
          />
          {!slugOk && d.name && <span className="mt-1 block text-2xs text-rose-500">Use letters, digits, _ or - only.</span>}
          {showErrors && !d.name && <span className="mt-1 block text-2xs text-rose-500">Required</span>}
        </Field>
      )}
      <Field label="Agent display name *">
        <Input
          value={d.agent_name}
          placeholder="Priya Shah"
          aria-invalid={showErrors && !d.agent_name ? true : undefined}
          className={cx(showErrors && !d.agent_name && 'ring-rose-400/50')}
          onChange={(e) => set({ agent_name: e.target.value })}
        />
        {showErrors && !d.agent_name && <span className="mt-1 block text-2xs text-rose-500">Required</span>}
      </Field>
      <Field label="Company name *">
        <Input
          value={d.company_name}
          placeholder="Acme Realty"
          aria-invalid={showErrors && !d.company_name ? true : undefined}
          className={cx(showErrors && !d.company_name && 'ring-rose-400/50')}
          onChange={(e) => set({ company_name: e.target.value })}
        />
        {showErrors && !d.company_name && <span className="mt-1 block text-2xs text-rose-500">Required</span>}
      </Field>
      <Field label="Company tagline">
        <Input value={d.company_tagline} placeholder="Homes you'll love" onChange={(e) => set({ company_tagline: e.target.value })} />
      </Field>
      <Field label="Role description">
        <Input value={d.role_description} placeholder="e.g. inbound sales consultant" onChange={(e) => set({ role_description: e.target.value })} />
      </Field>
      <Field label="Contact (phone)">
        <Input value={d.contact} placeholder="+91 22 1234 5678" onChange={(e) => set({ contact: e.target.value })} />
      </Field>
      <Field label="LLM model">
        <Input value={d.model} onChange={(e) => set({ model: e.target.value })} />
      </Field>
    </div>
  )
}

/* ───────────────────────── Call direction ─────────────────────────
 * Inbound and outbound calls demand opposite approaches, so every agent
 * explicitly declares which kind it handles. The runtime injects a strong
 * direction-specific playbook into the system prompt based on this. */
/* `locked`: an EXISTING twin's direction is its slug (`x-inbound` answers,
 * `x-outbound` places calls) and the server refuses a flip — RUNBOOK §60.
 * Show that here instead of letting the user fill the form and meet a 422. */
export function CallDirectionSection({ d, set, locked = false }: SectionProps & { locked?: boolean }) {
  const other = d.call_direction === 'outbound' ? 'inbound' : 'outbound'
  const opts: {
    value: WizardData['call_direction']
    label: string
    icon: typeof PhoneIncoming
    desc: string
  }[] = [
    {
      value: 'inbound',
      label: 'Inbound — answers calls',
      icon: PhoneIncoming,
      desc: 'Callers reach out to the agent. It welcomes them, listens first, resolves issues, requests and demands, answers questions, and schedules appointments and visits.',
    },
    {
      value: 'outbound',
      label: 'Outbound — places calls',
      icon: PhoneOutgoing,
      desc: 'The agent calls people, leads the conversation, pitches products/services, and resolves queries they asked to be called back about. It automatically greets contacts from Outbound Calls by name and confirms it reached the right person.',
    },
  ]
  return (
    <div className="space-y-3">
    <div className="grid gap-3 sm:grid-cols-2">
      {opts.map((o) => {
        const active = d.call_direction === o.value
        const Icon = o.icon
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={active}
            disabled={locked && !active}
            onClick={() => { if (!locked) set({ call_direction: o.value }) }}
            className={cx(
              'rounded-xl p-4 text-left ring-1 transition',
              active
                ? 'bg-mint-400/10 ring-mint-400/40'
                : 'bg-[var(--surface-2)] ring-[var(--border)] hover:ring-mint-400/20',
              locked && !active && 'cursor-not-allowed opacity-50 hover:ring-[var(--border)]',
            )}
          >
            <div className={cx('mb-1.5 flex items-center gap-2 text-sm font-semibold', active ? 'text-mint-700 dark:text-mint-300' : '')}>
              <Icon size={16} /> {o.label}
            </div>
            <div className="text-xs leading-relaxed text-soft">{o.desc}</div>
          </button>
        )
      })}
    </div>
    {locked && (
      <p className="text-xs text-soft">
        This is the {d.call_direction} version of the agent, and its call type is fixed by its name.
        To work on {other} calls, open the {other} version from the Agents page (or add it there).
      </p>
    )}
    </div>
  )
}

/* ───────────────────────── Calendar / time slots ─────────────────────────
 * Optional but strongly recommended: agents with a calendar can quote free
 * slots on calls and book the caller into one (bookings persist per agent,
 * and a booked slot stops being offered on later calls). */
export function CalendarSection({ d, set }: SectionProps) {
  const setSlot = (i: number, patch: Partial<{ day: string; time: string }>) => {
    const x = [...d.calendar_slots]
    x[i] = { ...x[i], ...patch }
    set({ calendar_slots: x })
  }
  return (
    <div className="space-y-4">
      <div className="rounded-xl bg-mint-400/10 p-3 text-xs leading-relaxed text-mint-700 ring-1 ring-mint-400/20 dark:text-mint-300">
        <span className="font-semibold">Recommended:</span> give the agent a calendar. It will mention free
        slots when callers ask about availability, offer at most two options at a time, and book the caller
        into a slot — booked slots are stored and no longer offered on later calls.
      </div>
      <button
        type="button"
        aria-pressed={d.calendar_enabled}
        onClick={() => {
          if (!d.calendar_enabled && d.calendar_slots.length === 0) {
            set({
              calendar_enabled: true,
              calendar_slots: [
                { day: 'Tomorrow', time: '10:00 AM' },
                { day: 'This Saturday', time: '11:00 AM' },
              ],
            })
          } else {
            set({ calendar_enabled: !d.calendar_enabled })
          }
        }}
        className={cx(
          'flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-semibold ring-1 transition',
          d.calendar_enabled
            ? 'bg-mint-400/15 text-mint-700 ring-mint-400/30 dark:text-mint-300'
            : 'bg-[var(--surface-2)] text-soft ring-[var(--border)]',
        )}
      >
        <CalendarDays size={15} />
        {d.calendar_enabled ? 'Calendar enabled' : 'Enable calendar'}
      </button>
      {d.calendar_enabled && (
        <div>
          <div className="mb-2 flex items-center justify-between">
            <div className="eyebrow">Available time slots</div>
            <Button variant="ghost" onClick={() => set({ calendar_slots: [...d.calendar_slots, { day: '', time: '' }] })}>
              <Plus size={14} /> Add slot
            </Button>
          </div>
          <div className="space-y-2">
            {d.calendar_slots.map((s, i) => (
              <Card key={i} className="grid gap-3 p-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
                <Field label="Day">
                  <Input value={s.day} placeholder="Tomorrow / This Saturday / Monday" onChange={(e) => setSlot(i, { day: e.target.value })} />
                </Field>
                <Field label="Time">
                  <Input value={s.time} placeholder="10:00 AM" onChange={(e) => setSlot(i, { time: e.target.value })} />
                </Field>
                <Button variant="ghost" title={`Remove slot ${i + 1}`} onClick={() => set({ calendar_slots: d.calendar_slots.filter((_, j) => j !== i) })}>
                  <Trash2 size={14} />
                </Button>
              </Card>
            ))}
            {d.calendar_slots.length === 0 && (
              <div className="text-xs text-dim">No slots yet — add at least one so the agent has availability to offer.</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/* ───────────────────────── Languages & voice ───────────────────────── */
export function LanguagesSection({ d, set }: SectionProps) {
  const toggleLang = (code: string) => {
    const has = d.supported.includes(code)
    const supported = has ? d.supported.filter((l) => l !== code) : [...d.supported, code]
    const default_lang = supported.includes(d.default_lang) ? d.default_lang : supported[0] || 'en'
    set({ supported, default_lang })
  }
  return (
    <div className="space-y-5">
      <div>
        <div className="eyebrow mb-2">Supported languages *</div>
        <div className="flex flex-wrap gap-2">
          {LANGS.map((l) => (
            <button
              key={l.code}
              type="button"
              aria-pressed={d.supported.includes(l.code)}
              onClick={() => toggleLang(l.code)}
              className={cx(
                'rounded-md px-3 py-1.5 text-sm font-semibold ring-1 transition',
                d.supported.includes(l.code)
                  ? 'bg-mint-400/15 text-mint-700 ring-mint-400/30 dark:text-mint-300'
                  : 'bg-[var(--surface-2)] text-soft ring-[var(--border)]',
              )}
            >
              {l.label} <span className="opacity-60">({l.code})</span>
            </button>
          ))}
        </div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Default language">
          <Select value={d.default_lang} onChange={(e) => set({ default_lang: e.target.value })}>
            {d.supported.map((l) => <option key={l} value={l}>{l}</option>)}
          </Select>
        </Field>
        {/* Pronouns are DERIVED from the voice (RUNBOOK §42): a male speaker
            speaks as a man, a female one as a woman — in the prompt and in
            every scripted line. A free choice here used to drift from the
            voice (seven shubh agents were set to "she"). Read-only; it
            refreshes from the speaker on save. */}
        <Field label="Pronouns">
          <Input value={d.pronouns || '—'} readOnly aria-readonly className="cursor-default opacity-80" />
          <span className="mt-1 block text-2xs text-dim">Follows the voice — set from the TTS speaker when you save.</span>
        </Field>
        <Field label="TTS model">
          <Input value={d.tts_model} onChange={(e) => set({ tts_model: e.target.value })} />
        </Field>
        <Field label="TTS speaker">
          <Input value={d.tts_speaker} onChange={(e) => set({ tts_speaker: e.target.value })} />
        </Field>
      </div>
    </div>
  )
}

/* ───────────────────────── Personality ───────────────────────── */
export function PersonalitySection({ d, set }: SectionProps) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label="Energy">
        <Select value={d.personality_energy} onChange={(e) => set({ personality_energy: e.target.value })}>
          <option value="low">low</option><option value="medium">medium</option><option value="high">high</option>
        </Select>
      </Field>
      <Field label="Tone">
        <Input value={d.personality_tone} onChange={(e) => set({ personality_tone: e.target.value })} />
      </Field>
      <Field label="Style">
        <Select value={d.personality_style} onChange={(e) => set({ personality_style: e.target.value })}>
          <option value="concise">concise</option><option value="natural">natural</option><option value="detailed">detailed</option>
        </Select>
      </Field>
      <Field label="Lingo / vocabulary notes">
        <Input value={d.personality_lingo} onChange={(e) => set({ personality_lingo: e.target.value })} />
      </Field>
      <div className="sm:col-span-2">
        <Field label="Custom instructions">
          <Textarea rows={3} value={d.personality_custom} onChange={(e) => set({ personality_custom: e.target.value })} />
        </Field>
      </div>
    </div>
  )
}

/* ───────────────────────── Knowledge ───────────────────────── */
export function KnowledgeSection({ d, set }: SectionProps) {
  return (
    <div className="space-y-5">
      <Field label="Locations / service areas (comma-separated)">
        <Input
          value={d.locations.join(', ')}
          placeholder="Mumbai, Pune, Ahmedabad"
          onChange={(e) => set({ locations: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })}
        />
      </Field>
      <Field label="Portfolio section label">
        <Input value={d.portfolio_label} onChange={(e) => set({ portfolio_label: e.target.value })} />
      </Field>
      <div>
        <div className="mb-2 flex items-center justify-between">
          <div className="eyebrow">Products / projects</div>
          <Button variant="ghost" onClick={() => set({ products: [...d.products, { name: '', price: '', description: '', features: '' }] })}>
            <Plus size={14} /> Add
          </Button>
        </div>
        <div className="space-y-3">
          {d.products.map((p, i) => (
            <Card key={i} className="grid gap-3 p-4 sm:grid-cols-2">
              <div className="sm:col-span-2 flex items-center gap-2">
                <span className="grid h-5 w-5 place-items-center rounded-full bg-indigo-500/12 text-2xs font-bold text-indigo-500 dark:text-indigo-300">{i + 1}</span>
                <span className="text-2xs font-semibold text-dim">Product {i + 1}</span>
              </div>
              <Field label="Name"><Input value={p.name} onChange={(e) => { const x = [...d.products]; x[i] = { ...x[i], name: e.target.value }; set({ products: x }) }} /></Field>
              <Field label="Price / tier"><Input value={p.price} onChange={(e) => { const x = [...d.products]; x[i] = { ...x[i], price: e.target.value }; set({ products: x }) }} /></Field>
              <div className="sm:col-span-2"><Field label="Description"><Input value={p.description} onChange={(e) => { const x = [...d.products]; x[i] = { ...x[i], description: e.target.value }; set({ products: x }) }} /></Field></div>
              <div className="sm:col-span-2"><Field label="Key features"><Input value={p.features} onChange={(e) => { const x = [...d.products]; x[i] = { ...x[i], features: e.target.value }; set({ products: x }) }} /></Field></div>
              <div className="sm:col-span-2 flex justify-end">
                <Button variant="ghost" title={`Remove product ${i + 1}`} onClick={() => set({ products: d.products.filter((_, j) => j !== i) })}><Trash2 size={14} /> Remove</Button>
              </div>
            </Card>
          ))}
          {d.products.length === 0 && (
            <div className="rounded-xl bg-[var(--surface-2)] p-4 ring-1 ring-[var(--border)]">
              <p className="text-xs text-dim">
                No products yet — each product's name, price and features become talking points the agent
                can quote on calls. Add at least one so it has something concrete to pitch.
              </p>
              <Button
                variant="soft"
                size="sm"
                className="mt-3"
                onClick={() => set({ products: [...d.products, { name: '', price: '', description: '', features: '' }] })}
              >
                <Plus size={13} /> Add first product
              </Button>
            </div>
          )}
        </div>
      </div>
      <Field label="Objection trigger phrases (comma-separated)">
        <Input
          value={d.objection_patterns.join(', ')}
          onChange={(e) => set({ objection_patterns: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })}
        />
      </Field>
    </div>
  )
}

/* ───────────────────────── Call script (opening line + stages) ─────────────
 * ONE home for everything the call says and does, so the exact spoken opening
 * and the AI's stage instructions never live in two clashing places:
 *  - Opening line  = response_bank.opening_greeting, spoken VERBATIM at
 *    connect. Supports the {contact_name} placeholder (outbound: replaced
 *    with the dialed contact's name from Outbound Calls).
 *  - Stage goals   = instructions the AI follows — never a word-for-word
 *    script; placeholders do NOT work there. */
// Same tolerant pattern the runtime accepts — so the Insert button doesn't
// nag when a variant form (${contact_name}, [name], …) is already present.
const PLACEHOLDER_RE = /(?:\$\{|\{\{|\{|\[|<)\s*(?:contact[ _-]?name|customer[ _-]?name|person[ _-]?name|callee[ _-]?name|contact|name)\s*(?:\}\}|\}|\]|>)|\$contact[ _-]?name\b/i

export function CallFlowSection({ d, set, showErrors = false }: SectionProps & { showErrors?: boolean }) {
  const outbound = d.call_direction === 'outbound'
  const hasToken = PLACEHOLDER_RE.test(d.opening_greeting)
  const moveStage = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (j < 0 || j >= d.stages.length) return
    const x = [...d.stages]
    ;[x[i], x[j]] = [x[j], x[i]]
    set({ stages: x })
  }
  return (
    <div className="space-y-6">
      <div>
        <Field label="Opening line — spoken word-for-word when the call connects *">
          <Textarea
            rows={2}
            value={d.opening_greeting}
            placeholder={outbound ? 'Hello, am I speaking with {contact_name}?' : 'Namaste! Thank you for calling Acme Realty…'}
            aria-invalid={showErrors && !d.opening_greeting ? true : undefined}
            className={cx(showErrors && !d.opening_greeting && 'ring-rose-400/50')}
            onChange={(e) => set({ opening_greeting: e.target.value })}
          />
        </Field>
        {showErrors && !d.opening_greeting && <span className="mt-1 block text-2xs text-rose-500">Required</span>}
        <span className="mt-1 block text-2xs text-dim">
          This exact sentence opens every call — the AI does not rephrase it.
          {outbound && (
            <> Write <span className="font-mono text-soft">{'{contact_name}'}</span> where the
            person's name should go — it is filled automatically from the Outbound Calls contact being dialed.</>
          )}
        </span>
        {outbound && !hasToken && (
          <Button
            variant="ghost"
            size="sm"
            className="mt-2"
            onClick={() => set({ opening_greeting: (d.opening_greeting ? d.opening_greeting.replace(/\s+$/, '') + ' ' : '') + '{contact_name}' })}
          >
            <Plus size={13} /> Insert {'{contact_name}'}
          </Button>
        )}
      </div>
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="eyebrow">Call stages (in order)</div>
          <Button variant="ghost" onClick={() => set({ stages: [...d.stages, { id: 'NEW_STAGE', goal: '', turns: '4' }] })}>
            <Plus size={14} /> Add stage
          </Button>
        </div>
        <p className="text-xs leading-relaxed text-dim">
          Stages are <span className="font-semibold text-soft">instructions the AI follows</span> as the call
          progresses — write each goal as directions ("Confirm you're speaking with the right person, then explain
          why you're calling"), not as a word-for-word line. The exact first sentence lives in the Opening line
          above; placeholders like {'{contact_name}'} only work there.
        </p>
        {d.stages.map((s, i) => (
          <Card key={i} className="grid gap-3 p-4 lg:grid-cols-[auto_150px_1fr_100px_auto] lg:items-start">
            <span className="grid h-5 w-5 place-items-center rounded-full bg-indigo-500/12 text-2xs font-bold text-indigo-500 dark:text-indigo-300 lg:mt-7">{i + 1}</span>
            <Field label="Stage ID"><Input value={s.id} onChange={(e) => { const x = [...d.stages]; x[i] = { ...x[i], id: e.target.value }; set({ stages: x }) }} /></Field>
            <Field label="Goal — instructions for this part of the call">
              <Textarea
                rows={2}
                value={s.goal}
                placeholder="What should the agent accomplish here, and which talking points should it use?"
                onChange={(e) => { const x = [...d.stages]; x[i] = { ...x[i], goal: e.target.value }; set({ stages: x }) }}
              />
            </Field>
            <div>
              <Field label="Max turns"><Input value={s.turns} onChange={(e) => { const x = [...d.stages]; x[i] = { ...x[i], turns: e.target.value }; set({ stages: x }) }} /></Field>
              <span className="mt-1 block text-2xs text-dim">99 = stay until the goal is met</span>
            </div>
            <div className="flex items-center gap-1.5 lg:mt-6 lg:gap-1">
              <IconButton label="Move up" disabled={i === 0} onClick={() => moveStage(i, -1)}><ChevronUp size={13} /></IconButton>
              <IconButton label="Move down" disabled={i === d.stages.length - 1} onClick={() => moveStage(i, 1)}><ChevronDown size={13} /></IconButton>
              <IconButton label="Remove stage" onClick={() => set({ stages: d.stages.filter((_, j) => j !== i) })}><Trash2 size={13} /></IconButton>
            </div>
          </Card>
        ))}
        {showErrors && d.stages.length === 0 && (
          <div className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
            At least one call stage is required.
          </div>
        )}
      </div>
    </div>
  )
}

/* ───────────────────────── Objections & rules ─────────────────────────
 * Canned responses for specific moments + always/never rules. The call's
 * opening line is configured in the Call Script section, not here. */
export function ResponsesSection({ d, set }: SectionProps) {
  return (
    <div className="space-y-4">
      <p className="text-xs text-dim">
        Fixed responses the agent uses at specific moments. The call's opening line is configured
        under <span className="font-semibold text-soft">Call Script</span>.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Objection response (English)"><Textarea rows={2} value={d.objection_response_en} onChange={(e) => set({ objection_response_en: e.target.value })} /></Field>
        <Field label="Objection response (Hindi)"><Textarea rows={2} value={d.objection_response_hi} onChange={(e) => set({ objection_response_hi: e.target.value })} /></Field>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Always rules (one per line)">
          <Textarea rows={4} value={d.always_rules.join('\n')} onChange={(e) => set({ always_rules: e.target.value.split('\n').map((s) => s.trim()).filter(Boolean) })} />
        </Field>
        <Field label="Never rules (one per line)">
          <Textarea rows={4} value={d.never_rules.join('\n')} onChange={(e) => set({ never_rules: e.target.value.split('\n').map((s) => s.trim()).filter(Boolean) })} />
        </Field>
      </div>
    </div>
  )
}

/* ───────────────────────── Routing fields ─────────────────────────
 * Same fields on the Edit → Routing tab and the Create → Routing step. */
export type TimeWindowDraft = { start: string; end: string }

export function RoutingFields({
  r, setR, win, setWin, disabled = false,
}: {
  r: Routing
  setR: (r: Routing) => void
  win: TimeWindowDraft
  setWin: (w: TimeWindowDraft) => void
  disabled?: boolean
}) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label="Inbound DID (E.164, blank = catch-all)">
        <Input value={r.did} placeholder="+912212345678" onChange={(e) => setR({ ...r, did: e.target.value })} disabled={disabled} />
      </Field>
      <Field label="Fallback agent (slug)">
        <Input value={r.fallback_agent} placeholder="pramukh" onChange={(e) => setR({ ...r, fallback_agent: e.target.value })} disabled={disabled} />
      </Field>
      <Field label="Caller-ID prefixes (comma-separated)">
        <Input value={(r.caller_id_prefixes || []).join(', ')} onChange={(e) => setR({ ...r, caller_id_prefixes: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} disabled={disabled} />
      </Field>
      <Field label="Active hours (start–end, 0–23)">
        <div className="flex items-center gap-2">
          <Input type="number" className="min-w-0 flex-1" value={win.start} placeholder="9" onChange={(e) => setWin({ ...win, start: e.target.value })} disabled={disabled} />
          <span className="text-dim">–</span>
          <Input type="number" className="min-w-0 flex-1" value={win.end} placeholder="21" onChange={(e) => setWin({ ...win, end: e.target.value })} disabled={disabled} />
        </div>
      </Field>
    </div>
  )
}
