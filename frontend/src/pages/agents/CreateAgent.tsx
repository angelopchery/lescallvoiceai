import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  buildAgentPayload, createAgent, putRouting,
  extractFromDocument, extractedFieldsToWizardPatch,
  DOC_ACCEPT, DOC_MAX_MB, DOC_TYPES_HUMAN, docSupported,
  type WizardData, type Routing, type MissingField,
} from '../../api/agents'
import { Card, Button, Badge, Spinner, SectionHead, cx } from '../../components/ui'
import { PageHeader } from '../../components/kit'
import { useToast } from '../../components/Toast'
import {
  BasicsSection, CallDirectionSection, CalendarSection, LanguagesSection,
  PersonalitySection, KnowledgeSection, CallFlowSection, ResponsesSection,
  RoutingFields, SLUG_RE, type TimeWindowDraft,
} from './AgentFormSections'
import { ChevronLeft, ChevronRight, FileUp, Wand2, Check, AlertTriangle, Info } from 'lucide-react'

const STEPS = ['Basics', 'Languages', 'Personality', 'Knowledge', 'Call Script', 'Calendar', 'Objections & Rules', 'Routing', 'Review']

// One-line purpose per step, shown as the in-card heading subtitle.
const STEP_SUBS: Record<string, string> = {
  'Basics': 'Who the agent is and which company it speaks for',
  'Languages': 'Which languages it speaks and which voice it uses',
  'Personality': 'Energy, tone and speaking style',
  'Knowledge': 'Locations, products and objection triggers it can draw on',
  'Call Script': 'The exact opening line plus stage-by-stage instructions',
  'Calendar': 'Optional — availability the agent can offer and book',
  'Objections & Rules': 'Canned responses plus always / never rules',
  'Routing': 'Optional — which inbound number reaches this agent',
  'Review': 'Check everything before creating the agent',
}

const DEFAULT_STAGES = [
  { id: 'GREETING', goal: 'Greet the caller warmly, introduce yourself, and ask how you can help.', turns: '2' },
  { id: 'DISCOVERY', goal: "Understand the caller's needs and qualify their interest.", turns: '4' },
  { id: 'PITCH', goal: 'Present the most relevant option and explain its value clearly.', turns: '4' },
  { id: 'OBJECTION', goal: 'Address concerns honestly and build confidence.', turns: '3' },
  { id: 'CLOSING', goal: 'Confirm the next step (visit/callback) and close politely.', turns: '99' },
]

// Outbound calls need a pitch-shaped flow, not the inbound welcome→discovery
// skeleton — mirrors the copilot backend's _DEFAULT_OUTBOUND_STAGES.
const DEFAULT_OUTBOUND_STAGES = [
  { id: 'OPENING', goal: "Confirm you're speaking with the right person, introduce yourself and who you represent in one short sentence, and ask if it's a good time to talk.", turns: '2' },
  { id: 'PITCH', goal: "State plainly why you're calling, then make the case using the strongest specific talking points — check they're following before adding more.", turns: '4' },
  { id: 'HANDLE_QUESTIONS', goal: 'Answer questions and push-back directly with concrete facts; acknowledge each concern first, stay warm and never defensive.', turns: '3' },
  { id: 'ASK', goal: 'Make the specific ask this call exists for, clearly and directly, and give them the chance to agree.', turns: '4' },
  { id: 'CLOSING', goal: 'Thank them by name, restate any agreed next step in one sentence, and close warmly.', turns: '99' },
]

const initial: WizardData = {
  name: '',
  agent_name: '',
  company_name: '',
  company_tagline: '',
  role_description: '', // blank → buildAgentPayload picks a direction-aware default
  contact: '',
  call_direction: 'inbound',
  model: 'gpt-4o-mini',
  supported: ['en'],
  default_lang: 'en',
  tts_model: 'bulbul:v3',
  tts_speaker: 'shubh',
  pronouns: 'she',
  personality_energy: 'medium',
  personality_tone: 'conversational',
  personality_style: 'natural',
  personality_lingo: '',
  personality_custom: '',
  locations: [],
  products: [{ name: '', price: '', description: '', features: '' }],
  portfolio_label: 'KNOWLEDGE BASE',
  objection_patterns: ['too expensive', 'not interested', 'call me later', 'thinking about it'],
  stages: DEFAULT_STAGES,
  opening_greeting: '',
  objection_response_en: '',
  objection_response_hi: '',
  always_rules: [],
  never_rules: [],
  // Calendar is optional but recommended — default ON with two sample slots
  // so every new agent can offer and book visit times unless deliberately
  // switched off.
  calendar_enabled: true,
  calendar_slots: [
    { day: 'Tomorrow', time: '10:00 AM' },
    { day: 'This Saturday', time: '11:00 AM' },
  ],
}

// Which wizard step collects each extraction field the document may lack —
// shown next to missing items so the user knows where they'll be asked.
const MISSING_STEP: Record<string, string> = {
  agent_name: 'Basics',
  company_name: 'Basics',
  company_tagline: 'Basics',
  role_description: 'Basics',
  contact: 'Basics',
  call_direction: 'Basics',
  portfolio_prompt_text: 'Knowledge',
  locations: 'Knowledge',
  appointment_slots: 'Calendar',
  opening_greeting: 'Call Script',
}

export default function CreateAgent() {
  const nav = useNavigate()
  const toast = useToast()
  const [step, setStep] = useState(0)
  const [d, setD] = useState<WizardData>(initial)
  const [r, setR] = useState<Routing>({ did: '', caller_id_prefixes: [], fallback_agent: '', time_windows: [] })
  const [win, setWin] = useState<TimeWindowDraft>({ start: '', end: '' })
  const [saving, setSaving] = useState(false)
  /** Set when the server refuses the create with a 402 plan-cap refusal. */
  const [capRefusal, setCapRefusal] = useState('')
  const [extracting, setExtracting] = useState(false)
  const [extractingName, setExtractingName] = useState('')
  const [dragging, setDragging] = useState(false)
  const [missing, setMissing] = useState<MissingField[] | null>(null)
  const [extractedFrom, setExtractedFrom] = useState('')
  const [attemptedNext, setAttemptedNext] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const headingRef = useRef<HTMLDivElement>(null)
  const railRef = useRef<HTMLDivElement>(null)
  const stepBodyRef = useRef<HTMLDivElement>(null)
  const createdRef = useRef(false)

  // Unsaved-draft guard: warn on tab close / refresh once anything was typed.
  const dirty = JSON.stringify(d) !== JSON.stringify(initial) || !!r.did
  useEffect(() => {
    if (!dirty) return
    const h = (e: BeforeUnloadEvent) => {
      if (createdRef.current) return
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [dirty])

  // On step change: scroll to top, move focus to the step heading, and keep
  // the active rail pill in view on small screens.
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'smooth' })
    headingRef.current?.focus({ preventScroll: true })
    railRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' })
    setAttemptedNext(false)
  }, [step])

  const set = (patch: Partial<WizardData>) => setD((p) => {
    const next = { ...p, ...patch }
    // Direction toggled while the stages are still an untouched default
    // skeleton → swap to the matching skeleton so outbound agents don't
    // ship an inbound "ask how you can help" flow (and vice versa).
    if (patch.call_direction && patch.call_direction !== p.call_direction) {
      const s = JSON.stringify(p.stages)
      if (s === JSON.stringify(DEFAULT_STAGES) || s === JSON.stringify(DEFAULT_OUTBOUND_STAGES)) {
        next.stages = patch.call_direction === 'outbound' ? DEFAULT_OUTBOUND_STAGES : DEFAULT_STAGES
      }
    }
    return next
  })

  const onDocument = async (file: File) => {
    // The server refuses these too (415 / 413 with the way out); checking
    // here saves uploading 40 MB to be told it is 20 MB too many.
    if (!docSupported(file.name)) {
      toast(`${file.name}: unsupported type — upload ${DOC_TYPES_HUMAN}`, 'error')
      if (fileRef.current) fileRef.current.value = ''
      return
    }
    if (file.size > DOC_MAX_MB * 1048576) {
      toast(`${file.name} is ${(file.size / 1048576).toFixed(1)} MB — the limit is ${DOC_MAX_MB} MB`, 'error')
      if (fileRef.current) fileRef.current.value = ''
      return
    }
    setExtracting(true)
    setExtractingName(file.name)
    try {
      const res = await extractFromDocument(file)
      const patch = extractedFieldsToWizardPatch(res.fields || {})
      set(patch)
      setMissing(res.missing || [])
      setExtractedFrom(res.filename || file.name)
      const got = Object.keys(patch).length
      toast(
        got
          ? `Prefilled ${got} field${got === 1 ? '' : 's'} from ${file.name}`
          : `No agent fields could be extracted from ${file.name}`,
        got ? 'success' : 'warn',
      )
    } catch (e: any) {
      toast(`Document extraction failed: ${e.message || e}`, 'error')
    } finally {
      setExtracting(false)
      setExtractingName('')
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const slugOk = SLUG_RE.test(d.name)
  const canCreate =
    slugOk && d.name && d.agent_name && d.company_name && d.opening_greeting &&
    d.supported.length > 0 && d.stages.length > 0

  const calendarOk =
    !d.calendar_enabled || d.calendar_slots.some((s) => s.day.trim() && s.time.trim())

  const stepValid = (s: number): boolean => {
    if (s === 0) return !!(d.name && slugOk && d.agent_name && d.company_name)
    if (s === 1) return d.supported.length > 0
    if (s === 4) return !!d.opening_greeting && d.stages.length > 0 // Call Script: opening line + at least one stage
    if (s === 5) return calendarOk
    return true
  }

  // Human-readable list of what still blocks the Next button on this step —
  // mirrors stepValid exactly so the disabled state is never a mystery.
  const blockers = (s: number): string[] => {
    if (s === 0) return [
      !d.name ? 'Slug' : !slugOk ? 'Valid slug (letters, digits, _ or -)' : '',
      !d.agent_name ? 'Agent display name' : '',
      !d.company_name ? 'Company name' : '',
    ].filter(Boolean)
    if (s === 1) return d.supported.length === 0 ? ['At least one language'] : []
    if (s === 4) return [
      !d.opening_greeting ? 'Opening line' : '',
      d.stages.length === 0 ? 'At least one call stage' : '',
    ].filter(Boolean)
    if (s === 5) return calendarOk ? [] : ['One complete slot (day + time), or disable the calendar']
    return []
  }

  // A rail pill ahead of the current step is reachable when every step in
  // between passes validation.
  const canJumpTo = (target: number): boolean => {
    if (target <= step) return true
    for (let j = step; j < target; j++) if (!stepValid(j)) return false
    return true
  }

  // Next is always enabled (keyboard-reachable): clicking it on an invalid
  // step surfaces the inline errors and moves focus to the first blocker
  // instead of silently doing nothing behind a disabled button.
  const onNext = () => {
    if (stepValid(step)) {
      setStep((s) => s + 1)
      return
    }
    setAttemptedNext(true)
    // Wait a frame so the aria-invalid markers render, then focus the first one.
    requestAnimationFrame(() => {
      const el = stepBodyRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        el.focus({ preventScroll: true })
      } else {
        headingRef.current?.focus()
      }
    })
  }

  const submit = async () => {
    setSaving(true)
    try {
      const payload = buildAgentPayload({
        ...d,
        products: d.products.filter((p) => p.name.trim()),
      })
      // Creation makes a pair — res.name is the primary slug (base + a
      // -inbound/-outbound suffix), never the base name typed in the form.
      const res = await createAgent(payload)
      const slug = res?.name || d.name

      // Routing — applied after creation; skipped silently when all fields empty.
      const time_windows = win.start !== '' && win.end !== ''
        ? [{ start: Number(win.start), end: Number(win.end) }]
        : []
      const hasRouting = !!(r.did || (r.caller_id_prefixes || []).length || r.fallback_agent || time_windows.length)
      if (hasRouting) {
        try {
          await putRouting(slug, {
            did: r.did || '',
            caller_id_prefixes: r.caller_id_prefixes || [],
            fallback_agent: r.fallback_agent || '',
            time_windows,
          })
        } catch (e: any) {
          toast(`Agent created, but routing save failed: ${e.message || e}`, 'error')
        }
      }

      toast(`Agents "${res?.twins?.inbound || `${d.name}-inbound`}" and "${res?.twins?.outbound || `${d.name}-outbound`}" created — connect one to a number to go live`, 'success')
      createdRef.current = true
      nav(`/build/agents/${slug}`)
    } catch (e: any) {
      // A plan-cap refusal is a 402 carrying `code` and `reason`. Toasting it
      // as "Create failed: 402 …" reads as a server fault and invites a retry
      // that cannot succeed — say what the limit is and where to change it.
      if (e?.status === 402) {
        setCapRefusal(e.reason || e.message || 'Your plan does not allow another agent.')
        toast(e.reason || 'Agent limit reached', 'error')
      } else {
        toast(`Create failed: ${e.message || e}`, 'error')
      }
      setStep(STEPS.length - 1)
    } finally {
      setSaving(false)
    }
  }

  // Missing fields the user has since filled in drop off the list live.
  const stillMissing = (missing ?? []).filter((m) => {
    switch (m.key) {
      case 'agent_name': return !d.agent_name
      case 'company_name': return !d.company_name
      case 'company_tagline': return !d.company_tagline
      case 'role_description': return !d.role_description
      case 'contact': return !d.contact
      case 'call_direction': return false // always has a value; selector is on Basics
      case 'opening_greeting': return !d.opening_greeting
      case 'portfolio_prompt_text': return !d.products.some((p) => p.name.trim())
      case 'locations': return d.locations.length === 0
      case 'appointment_slots': return d.calendar_enabled && !d.calendar_slots.some((s) => s.day.trim() && s.time.trim())
      default: return true
    }
  })

  return (
    <div className="space-y-6">
      <PageHeader title="Create Agent" sub="Define a new voice agent step by step" />

      {capRefusal && (
        <Card className="border-amber-300 bg-amber-50 p-4 dark:border-amber-900/60 dark:bg-amber-950/30">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <div>
              <p className="text-sm font-semibold">Agent limit reached</p>
              <p className="mt-1 text-sm text-soft">{capRefusal}</p>
              <p className="mt-2 text-xs text-dim">
                Nothing you have entered is lost — your existing agents keep
                working, and archiving one frees a slot immediately.
              </p>
              <div className="mt-3 flex gap-2">
                <a href="/billing"><Button variant="primary" size="sm">See plans</Button></a>
                <a href="/build/agents"><Button variant="ghost" size="sm">Manage agents</Button></a>
              </div>
            </div>
          </div>
        </Card>
      )}

      {/* Start from a document — also a drop target */}
      <div
        className={cx('card flex flex-wrap items-center gap-3 p-4 transition', dragging && 'ring-2 ring-mint-400/40')}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          const f = e.dataTransfer.files?.[0]
          if (f && !extracting) onDocument(f)
        }}
      >
        <div className="flex items-center gap-2 text-sm font-semibold">
          <FileUp size={16} className="text-mint-500" /> Start from a document
        </div>
        <div className="text-xs text-dim">
          Upload or drop a company profile, brochure or call script ({DOC_TYPES_HUMAN}, up to {DOC_MAX_MB} MB) —
          every field it contains is extracted and prefilled; anything it lacks, you'll be asked for below.
        </div>
        <input
          ref={fileRef}
          type="file"
          accept={DOC_ACCEPT}
          className="hidden"
          onChange={(e) => e.target.files?.[0] && onDocument(e.target.files[0])}
        />
        <Button variant="soft" size="sm" onClick={() => fileRef.current?.click()} disabled={extracting}>
          {extracting ? <><Spinner /> Extracting…</> : 'Upload document'}
        </Button>
        {extracting && (
          <div className="flex w-full items-center gap-2">
            <div className="skeleton h-2 w-40" />
            <span className="min-w-0 flex-1 truncate text-xs text-dim">Reading {extractingName}…</span>
          </div>
        )}
        {!extracting && extractedFrom && (
          <Badge tone="mint" soft><Check size={12} /> <span className="max-w-[60vw] truncate">Prefilled from {extractedFrom}</span></Badge>
        )}
      </div>

      {/* Missing-info panel — the "ask the user for the rest" half of extraction */}
      {missing !== null && stillMissing.length > 0 && (
        <div className="rounded-xl bg-amber-500/10 p-4 ring-1 ring-amber-500/20">
          <div className="mb-2 flex items-start gap-2 text-sm font-semibold text-amber-700 sm:items-center dark:text-amber-300">
            <AlertTriangle size={15} className="shrink-0" /> <span className="min-w-0 break-words">{extractedFrom} didn't contain everything — please provide:</span>
          </div>
          <ul className="space-y-1">
            {stillMissing.map((m) => {
              const target = MISSING_STEP[m.key] ? STEPS.indexOf(MISSING_STEP[m.key]) : -1
              const body = (
                <>
                  <span className="font-semibold">{m.label}</span>
                  {MISSING_STEP[m.key] ? <span className="opacity-70"> · {MISSING_STEP[m.key]} step</span> : null}
                  <span className="opacity-70"> — {m.hint}</span>
                </>
              )
              return (
                <li key={m.key} className="text-xs text-amber-700/90 dark:text-amber-300/90">
                  {target >= 0 ? (
                    <button
                      type="button"
                      onClick={() => setStep(target)}
                      className="block w-full py-1 text-left underline decoration-dotted underline-offset-2 hover:text-amber-800 sm:inline-block sm:w-auto sm:py-0 dark:hover:text-amber-200"
                    >
                      {body}
                    </button>
                  ) : body}
                </li>
              )
            })}
          </ul>
        </div>
      )}
      {missing !== null && stillMissing.length === 0 && (
        <div className="rounded-xl bg-mint-400/10 px-4 py-3 text-xs font-medium text-mint-700 ring-1 ring-mint-400/20 dark:text-mint-300">
          All essential fields are covered — review each step and create the agent.
        </div>
      )}

      {/* Step rail — wraps on desktop, horizontal snap rail on phones */}
      <div ref={railRef} className="flex snap-x items-center gap-2 overflow-x-auto pb-1 [-webkit-overflow-scrolling:touch] sm:flex-wrap sm:overflow-visible">
        {STEPS.map((s, i) => {
          const reachable = canJumpTo(i)
          return (
            <button
              key={s}
              type="button"
              data-active={i === step ? 'true' : undefined}
              aria-current={i === step ? 'step' : undefined}
              disabled={i > step && !reachable}
              onClick={() => i !== step && reachable && setStep(i)}
              className={cx(
                'flex min-h-[34px] shrink-0 snap-start items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold ring-1 transition sm:min-h-0 sm:py-1',
                i === step
                  ? 'bg-mint-400/15 text-mint-700 ring-mint-400/30 dark:text-mint-300'
                  : i < step || reachable
                  ? 'bg-[var(--surface-2)] text-soft ring-[var(--border)] hover:text-[var(--text)]'
                  : 'bg-[var(--surface-2)] text-dim ring-[var(--border)] cursor-default',
              )}
            >
              <span className={cx('grid h-4 w-4 place-items-center rounded-full text-2xs', i < step ? 'bg-mint-500 text-white' : 'bg-[var(--surface-solid)] ring-1 ring-[var(--border)]')}>
                {i < step ? <Check size={10} /> : i + 1}
              </span>
              {s}
            </button>
          )
        })}
      </div>

      <Card className="p-4 sm:p-6">
        <div key={step} ref={stepBodyRef} className="animate-fade-up">
        <div ref={headingRef} tabIndex={-1} className="mb-1 outline-none">
          <div className="eyebrow mb-1">Step {step + 1} of {STEPS.length}</div>
          <SectionHead title={STEPS[step]} sub={STEP_SUBS[STEPS[step]]} />
        </div>
        {step === 0 && (
          <div className="space-y-6">
            <div className="flex items-start gap-2 rounded-xl bg-indigo-500/10 px-4 py-3 ring-1 ring-indigo-500/20">
              <Info size={14} className="mt-0.5 shrink-0 text-indigo-500" />
              <p className="text-xs text-soft">
                The slug you enter is a <span className="font-semibold">base name</span> — two agents are
                created from it:{' '}
                <code className="font-mono text-2xs">{d.name || 'your-slug'}-inbound</code> (answers
                incoming calls) and{' '}
                <code className="font-mono text-2xs">{d.name || 'your-slug'}-outbound</code> (speaks on
                outgoing calls). Both start from what you fill in here and can be edited separately
                afterwards.
              </p>
            </div>
            <BasicsSection d={d} set={set} showErrors={attemptedNext} />
            <div>
              <div className="eyebrow mb-2">Call type *</div>
              <CallDirectionSection d={d} set={set} />
            </div>
          </div>
        )}
        {step === 1 && <LanguagesSection d={d} set={set} />}
        {step === 2 && <PersonalitySection d={d} set={set} />}
        {step === 3 && <KnowledgeSection d={d} set={set} />}
        {step === 4 && <CallFlowSection d={d} set={set} showErrors={attemptedNext} />}
        {step === 5 && <CalendarSection d={d} set={set} />}
        {step === 6 && <ResponsesSection d={d} set={set} />}

        {step === 7 && (
          <div className="space-y-4">
            <p className="text-xs text-dim">
              Optional — decide which inbound number this agent answers. Leave everything blank to skip;
              you can configure routing later from the agent's Routing tab.
            </p>
            <RoutingFields r={r} setR={setR} win={win} setWin={setWin} />
          </div>
        )}

        {step === 8 && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="indigo" soft>
                <span className="max-w-[60vw] truncate" title={`${d.name}-inbound and ${d.name}-outbound`}>
                  {d.name}-inbound + -outbound
                </span>
              </Badge>
              <Badge tone="mint" soft><span className="max-w-[60vw] truncate">{d.agent_name}</span></Badge>
              <Badge tone={d.call_direction === 'outbound' ? 'amber' : 'mint'} soft>{d.call_direction}</Badge>
              <span className="text-sm text-soft">{d.company_name}</span>
              <span className="ml-auto text-xs text-dim">
                {d.supported.join(' · ')} · {d.stages.length} stages · {d.products.filter((p) => p.name).length} products
                {d.calendar_enabled ? ` · ${d.calendar_slots.filter((s) => s.day && s.time).length} slots` : ' · no calendar'}
              </span>
            </div>

            {/* Per-section summary tiles with Edit jump links */}
            <div className="grid gap-3 sm:grid-cols-2">
              {([
                { title: 'Basics', target: 0, body: `${d.agent_name || '—'} · ${d.company_name || '—'} · ${d.model}` },
                { title: 'Languages', target: 1, body: `${d.supported.join(', ') || '—'} · default ${d.default_lang} · voice ${d.tts_speaker}` },
                { title: 'Personality', target: 2, body: `${d.personality_energy} energy · ${d.personality_tone || '—'} · ${d.personality_style}` },
                { title: 'Knowledge', target: 3, body: `${d.locations.length} locations · ${d.products.filter((p) => p.name.trim()).length} products · ${d.objection_patterns.length} objection triggers` },
                { title: 'Call Script', target: 4, body: d.opening_greeting ? `"${d.opening_greeting}" · ${d.stages.length} stages` : `No opening line yet · ${d.stages.length} stages` },
                { title: 'Calendar', target: 5, body: d.calendar_enabled ? `${d.calendar_slots.filter((s) => s.day.trim() && s.time.trim()).length} slots` : 'disabled' },
                { title: 'Objections & Rules', target: 6, body: `${d.always_rules.length} always · ${d.never_rules.length} never rules` },
                { title: 'Routing', target: 7, body: r.did || 'catch-all' },
              ] as { title: string; target: number; body: string }[]).map((t) => (
                <div key={t.title} className="rounded-xl bg-[var(--surface-2)] p-3 ring-1 ring-[var(--border)]">
                  <div className="flex items-center justify-between">
                    <span className="eyebrow">{t.title}</span>
                    <Button variant="ghost" size="sm" onClick={() => setStep(t.target)}>Edit</Button>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-soft line-clamp-2">{t.body}</p>
                </div>
              ))}
            </div>

            {!canCreate && (
              <div className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-300">
                Fill in{' '}
                <button type="button" className="font-semibold underline underline-offset-2" onClick={() => setStep(0)}>slug, agent name, company</button>
                {' '}and an{' '}
                <button type="button" className="font-semibold underline underline-offset-2" onClick={() => setStep(4)}>opening line with at least one call stage</button>
                {' '}before creating.
              </div>
            )}
            {!d.calendar_enabled && (
              <div className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-300">
                No calendar configured — the agent won't be able to offer or book visit slots. Recommended:
                go back to the{' '}
                <button type="button" className="font-semibold underline underline-offset-2" onClick={() => setStep(5)}>Calendar step</button>
                {' '}and add availability.
              </div>
            )}
            <details className="rounded-xl bg-[var(--surface-2)] p-4 ring-1 ring-[var(--border)]">
              <summary className="cursor-pointer text-xs font-semibold text-soft">Preview generated definition (JSON)</summary>
              <pre className="mt-3 max-h-80 overflow-auto text-2xs leading-relaxed text-dim">{JSON.stringify(buildAgentPayload({ ...d, products: d.products.filter((p) => p.name.trim()) }), null, 2)}</pre>
            </details>
          </div>
        )}

        </div>

        {/* Footer nav */}
        <div className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] pt-4">
          <Button variant="ghost" onClick={() => setStep((s) => Math.max(0, s - 1))} disabled={step === 0}>
            <ChevronLeft size={15} /> Back
          </Button>
          {step < STEPS.length - 1 ? (
            <div className="flex flex-wrap items-center justify-end gap-3">
              {!stepValid(step) && (
                <span aria-live="polite" className="text-xs font-medium text-amber-600 dark:text-amber-300">
                  Required to continue: {blockers(step).join(', ')}
                </span>
              )}
              <Button variant="primary" onClick={onNext}>
                Next: {STEPS[step + 1]} <ChevronRight size={15} />
              </Button>
            </div>
          ) : (
            <Button variant="primary" onClick={submit} disabled={!canCreate || saving}>
              <Wand2 size={15} /> {saving ? 'Creating…' : 'Create Agent'}
            </Button>
          )}
        </div>
      </Card>
    </div>
  )
}
