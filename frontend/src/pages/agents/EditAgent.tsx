import { useEffect, useRef, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useGet, useRefresh } from '../../api/hooks/useApi'
import {
  getAgent, updateAgent, agentToWizardData, mergeWizardIntoDefinition,
  getRawYaml, putRawYaml, validateRaw, getRouting, putRouting,
  uploadDoc, deleteDoc, extractYaml, applyYaml, deleteBooking, reindexDocs,
  DOC_ACCEPT, DOC_MAX_MB, DOC_TYPES_HUMAN, docSupported,
  archiveAgent, archiveConfirmCopy, ttsPreviewUrl,
  listRevisions, rollbackAgent, patchAgentOps,
  type Routing, type DocsList, type WizardData, type MissingField, type Booking,
  type AgentRevision, type AgentOp,
} from '../../api/agents'
import { Card, Button, Badge, Spinner, IconButton, cx, EmptyState } from '../../components/ui'
import { PageHeader, Field, Input, Select, Textarea, AsyncState } from '../../components/kit'
import { useToast } from '../../components/Toast'
import { useConfirm, usePrompt } from '../../components/confirm'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  BasicsSection, CallDirectionSection, CalendarSection, LanguagesSection,
  PersonalitySection, KnowledgeSection, CallFlowSection, ResponsesSection,
  RoutingFields, type TimeWindowDraft,
} from './AgentFormSections'
import {
  Radio, Archive, ArrowLeft, Save, CheckCircle2, AlertTriangle, Volume2,
  Upload, Trash2, FileText, Sparkles, Code2, Network, Files, SlidersHorizontal,
  CalendarDays, History, ListOrdered, ArrowUp, ArrowDown, Plus, RotateCcw,
  RefreshCw, FileUp, X,
} from 'lucide-react'
import { fmtDateTime } from '../../lib/format'

type Tab = 'settings' | 'stages' | 'yaml' | 'routing' | 'documents' | 'voice' | 'history'
const BCP47: Record<string, string> = { en: 'en-IN', hi: 'hi-IN', gu: 'gu-IN', mr: 'hi-IN' }

/* Three-row loading placeholder for tab bodies — mirrors the list layout the
   data lands in, replacing the bare centered Spinner. */
function TabSkeleton() {
  return (
    <div className="space-y-2">
      {[0, 1, 2].map((i) => <div key={i} className="skeleton h-11 w-full" />)}
    </div>
  )
}

export default function EditAgent() {
  const { name = '' } = useParams()
  const nav = useNavigate()
  const toast = useToast()
  const refresh = useRefresh()
  const confirm = useConfirm()
  const [tab, setTab] = useState<Tab>('settings')
  // Unsaved-edit guard: tabs unmount on switch, so a dirty Settings/YAML tab
  // would silently lose its edits without this.
  const dirtyRef = useRef(false)
  const setDirty = (v: boolean) => { dirtyRef.current = v }
  const switchTab = async (t: Tab) => {
    if (t === tab) return
    if (dirtyRef.current) {
      const ok = await confirm({
        title: 'Discard unsaved changes?',
        description: 'This tab has unsaved changes — switching tabs will discard them.',
        confirmLabel: 'Discard',
        destructive: true,
      })
      if (!ok) return
    }
    dirtyRef.current = false
    setTab(t)
  }
  useEffect(() => {
    const h = (e: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [])

  const { data: agent, isLoading, error } = useGet<any>(['agent', name], `/agents/${name}`)

  const onArchive = async () => {
    const ok = await confirm({
      title: `Archive "${name}"?`,
      description: archiveConfirmCopy(name),
      confirmLabel: 'Archive',
      destructive: true,
    })
    if (!ok) return
    try { await archiveAgent(name); toast(`Archived "${name}"`, 'success'); nav('/build/agents') }
    catch (e: any) { toast(`Archive failed: ${e.message || e}`, 'error') }
  }

  const tabs: { id: Tab; label: string; icon: any }[] = [
    { id: 'settings', label: 'Settings', icon: SlidersHorizontal },
    { id: 'stages', label: 'Stages', icon: ListOrdered },
    { id: 'yaml', label: 'Advanced (YAML)', icon: Code2 },
    { id: 'routing', label: 'Routing', icon: Network },
    { id: 'documents', label: 'Documents · RAG', icon: Files },
    { id: 'voice', label: 'Voice', icon: Volume2 },
    { id: 'history', label: 'History', icon: History },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title={agent?.identity?.agent_name || name}
        sub={agent?.identity?.company_name || 'Edit agent'}
        actions={
          <div className="flex items-center gap-2">
            <Link to="/build/agents"><Button variant="ghost"><ArrowLeft size={15} /> Agents</Button></Link>
            <Button variant="danger" title="Archive agent" onClick={() => void onArchive()}>
              <Archive size={14} /><span className="hidden sm:inline">Archive</span>
            </Button>
          </div>
        }
      />

      <AsyncState loading={isLoading} error={error}>
        <Tabs value={tab} onValueChange={(v) => { void switchTab(v as Tab) }}>
          <TabsList aria-label="Agent editor sections" className="h-auto flex-wrap justify-start gap-2 bg-transparent p-0">
            {tabs.map((t) => (
              <TabsTrigger
                key={t.id}
                value={t.id}
                className="gap-1.5 rounded-md bg-[var(--surface-2)] px-3 py-1.5 text-sm font-semibold text-soft ring-1 ring-[var(--border)] transition hover:text-[var(--text)] data-[state=active]:bg-mint-400/15 data-[state=active]:text-mint-700 data-[state=active]:shadow-none data-[state=active]:ring-mint-400/30 dark:data-[state=active]:text-mint-300"
              >
                <t.icon size={14} /> {t.label}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="settings" className="mt-6">
            <SettingsTab name={name} onSaved={() => refresh(['agent', name], ['agents'])} onDirtyChange={setDirty} />
          </TabsContent>
          <TabsContent value="stages" className="mt-6">
            <StagesTab name={name} onSaved={() => refresh(['agent', name], ['agents'])} />
          </TabsContent>
          <TabsContent value="yaml" className="mt-6">
            <YamlTab name={name} onSaved={() => refresh(['agent', name], ['agents'])} onDirtyChange={setDirty} />
          </TabsContent>
          <TabsContent value="routing" className="mt-6">
            <RoutingTab name={name} />
          </TabsContent>
          <TabsContent value="documents" className="mt-6">
            <DocumentsTab name={name} />
          </TabsContent>
          <TabsContent value="voice" className="mt-6">
            <VoiceTab agent={agent} />
          </TabsContent>
          <TabsContent value="history" className="mt-6">
            <HistoryTab name={name} onChanged={() => refresh(['agent', name], ['agents'])} />
          </TabsContent>
        </Tabs>
      </AsyncState>
    </div>
  )
}

/* ───────────────────────── Settings tab ─────────────────────────
 * Structured form built from the same shared sections as the Create wizard.
 * DATA SAFETY — merge-on-save: on Save we re-fetch the CURRENT full agent
 * definition and deep-merge only the sections this form models onto it via
 * mergeWizardIntoDefinition(), then PUT the merged FULL object. Fields the
 * form does not model (calendar, qa_field_signals, stt_corrections, slots,
 * register_notes wording, stt_forced_language, allow_code_mixing, pace, …)
 * therefore always survive an edit round-trip. */
const SETTINGS_SECTIONS = [
  { id: 'sec-basics', label: 'Basics' },
  { id: 'sec-call-type', label: 'Call Type' },
  { id: 'sec-languages', label: 'Languages & Voice' },
  { id: 'sec-personality', label: 'Personality' },
  { id: 'sec-script', label: 'Call Script' },
  { id: 'sec-knowledge', label: 'Knowledge' },
  { id: 'sec-calendar', label: 'Calendar' },
  { id: 'sec-rules', label: 'Objections & Rules' },
]

function SettingsTab({ name, onSaved, onDirtyChange }: { name: string; onSaved: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const toast = useToast()
  const [d, setD] = useState<WizardData | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [showErrors, setShowErrors] = useState(false)
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null)
  const [justSaved, setJustSaved] = useState(false)
  const initRef = useRef<WizardData | null>(null)

  useEffect(() => {
    let on = true
    getAgent(name)
      .then((def) => {
        if (!on) return
        const w = agentToWizardData(def, name)
        initRef.current = JSON.parse(JSON.stringify(w))
        setD(w)
      })
      .catch((e) => { if (on) setLoadError(e.message || String(e)) })
    return () => { on = false }
  }, [name])

  const set = (patch: Partial<WizardData>) => setD((p) => (p ? { ...p, ...patch } : p))

  const dirty = !!d && !!initRef.current && JSON.stringify(d) !== JSON.stringify(initRef.current)
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])

  // Per-section validity — powers the inline "needs attention" markers.
  const invalid = {
    'sec-basics': !!d && (!d.agent_name || !d.company_name),
    'sec-languages': !!d && d.supported.length === 0,
    'sec-script': !!d && (!d.opening_greeting || d.stages.length === 0),
  } as Record<string, boolean>

  const onSave = async () => {
    if (!d || !initRef.current) return
    if (!d.agent_name || !d.company_name || !d.opening_greeting || d.supported.length === 0 || d.stages.length === 0) {
      setShowErrors(true)
      toast('Agent name, company, opening greeting, at least one language and one stage are required', 'error')
      const firstInvalid = SETTINGS_SECTIONS.find((s) => invalid[s.id])?.id
      if (firstInvalid) document.getElementById(firstInvalid)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    setSaving(true)
    try {
      const fresh = await getAgent(name) // merge onto the LATEST definition, not a stale copy
      const rev: string | undefined = fresh?._rev
      delete fresh._rev
      const merged = mergeWizardIntoDefinition(fresh, d, initRef.current)
      delete merged._rev
      await updateAgent(name, merged, rev)
      initRef.current = JSON.parse(JSON.stringify(d))
      setD((p) => (p ? { ...p } : p)) // re-render so dirty recomputes against the new snapshot
      setShowErrors(false)
      setLastSavedAt(new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }))
      setJustSaved(true)
      setTimeout(() => setJustSaved(false), 1500)
      toast('Settings saved — effective on the next call', 'success')
      onSaved()
    } catch (e: any) {
      if (String(e?.message || e).startsWith('409')) {
        toast('This agent changed while you were editing (e.g. by the Copilot). '
          + 'Your form is untouched — press Save again to merge onto the latest version.', 'error')
      } else {
        toast(`Save failed: ${e.message || e}`, 'error')
      }
    } finally {
      setSaving(false)
    }
  }

  const saveBtn = (
    <Button variant="primary" onClick={onSave} disabled={!d || saving}>
      {justSaved ? <><CheckCircle2 size={14} /> Saved</> : <><Save size={14} /> {saving ? 'Saving…' : 'Save settings'}</>}
    </Button>
  )

  const sectionHead = (id: string, label: string) => (
    <div className="eyebrow mb-4 flex items-center gap-2">
      {label}
      {showErrors && invalid[id] && <Badge tone="rose" soft>needs attention</Badge>}
    </div>
  )
  const cardCls = (id: string) => cx('card p-4 sm:p-6 scroll-mt-24', showErrors && invalid[id] && 'ring-1 ring-rose-500/40')

  return (
    <AsyncState loading={!d && !loadError} error={loadError}>
      {d && (
        <div className="space-y-4 scroll-smooth">
          <Card className="flex flex-wrap items-center gap-3 p-4">
            <p className="min-w-[240px] flex-1 text-xs text-dim">
              Structured editor for the common agent fields. Anything not shown here (STT corrections,
              QA signals, per-language register notes, …) is preserved untouched —
              use the <span className="font-semibold text-soft">Advanced (YAML)</span> tab for those.
            </p>
            {saveBtn}
          </Card>

          {/* Section jump nav — 8 stacked cards need a map */}
          <nav aria-label="Settings sections" className="sticky top-2 z-10 flex gap-1.5 overflow-x-auto rounded-xl glass px-3 py-2 sm:flex-wrap sm:overflow-visible">
            {SETTINGS_SECTIONS.map((s) => (
              <a key={s.id} href={`#${s.id}`}
                className="shrink-0 whitespace-nowrap rounded-md bg-[var(--surface-2)] px-3 py-2 text-xs font-semibold text-soft ring-1 ring-[var(--border)] transition hover:text-[var(--text)] sm:px-2.5 sm:py-1">
                {s.label}
              </a>
            ))}
          </nav>

          <div id="sec-basics" className={cardCls('sec-basics')}>{sectionHead('sec-basics', 'Basics')}<BasicsSection d={d} set={set} showSlug={false} showErrors={showErrors} /></div>
          <div id="sec-call-type" className="card p-4 sm:p-6 scroll-mt-24"><div className="eyebrow mb-4">Call Type</div><CallDirectionSection d={d} set={set} locked={/-(inbound|outbound)$/.test(name)} /></div>
          <div id="sec-languages" className={cardCls('sec-languages')}>
            {sectionHead('sec-languages', 'Languages & Voice')}
            <LanguagesSection d={d} set={set} />
            {showErrors && invalid['sec-languages'] && <span className="mt-2 block text-2xs text-rose-500">Select at least one language.</span>}
          </div>
          <div id="sec-personality" className="card p-4 sm:p-6 scroll-mt-24"><div className="eyebrow mb-4">Personality</div><PersonalitySection d={d} set={set} /></div>
          <div id="sec-script" className={cardCls('sec-script')}>{sectionHead('sec-script', 'Call Script — Opening & Stages')}<CallFlowSection d={d} set={set} showErrors={showErrors} /></div>
          <div id="sec-knowledge" className="card p-4 sm:p-6 scroll-mt-24"><div className="eyebrow mb-4">Knowledge</div><KnowledgeSection d={d} set={set} /></div>
          <div id="sec-calendar" className="card p-4 sm:p-6 scroll-mt-24">
            <div className="eyebrow mb-4">Calendar &amp; Appointments</div>
            <CalendarSection d={d} set={set} />
            <BookingsList name={name} />
          </div>
          <div id="sec-rules" className="card p-4 sm:p-6 scroll-mt-24"><div className="eyebrow mb-4">Objections &amp; Rules</div><ResponsesSection d={d} set={set} /></div>

          {/* Sticky save bar — reachable from anywhere in the long form */}
          <div className="sticky bottom-4 z-10 flex flex-wrap items-center justify-end gap-3 rounded-2xl glass px-4 py-3">
            {dirty && <Badge tone="amber" soft>Unsaved changes</Badge>}
            {lastSavedAt && <span className="hidden sm:inline text-xs text-dim">Saved {lastSavedAt} — effective on the next call</span>}
            {saveBtn}
          </div>
        </div>
      )}
    </AsyncState>
  )
}

/* ───────────────────────── Bookings (calendar) ─────────────────────────
 * Appointments booked by live calls (agents/<slug>/bookings.json). Deleting
 * one frees its slot so the agent offers it again. */
function BookingsList({ name }: { name: string }) {
  const toast = useToast()
  const refresh = useRefresh()
  const confirm = useConfirm()
  const { data } = useGet<{ bookings: Booking[] }>(['bookings', name], `/agents/${name}/bookings`, { refetchInterval: 30000 })
  const bookings = data?.bookings ?? []

  const onFree = async (i: number) => {
    const ok = await confirm({
      title: 'Free this slot?',
      description: 'The booking is deleted and the agent starts offering the slot again on future calls.',
      confirmLabel: 'Free slot',
      destructive: true,
    })
    if (!ok) return
    try { await deleteBooking(name, i); toast('Slot freed', 'success'); refresh(['bookings', name]) }
    catch (e: any) { toast(`Failed: ${e.message || e}`, 'error') }
  }

  if (!data) return null
  return (
    <div className="mt-5">
      <div className="eyebrow mb-2 flex items-center gap-1.5"><CalendarDays size={13} /> Booked slots ({bookings.length})</div>
      {bookings.length === 0 && (
        <div className="rounded-lg bg-[var(--surface-2)] px-3 py-2 text-xs text-dim ring-1 ring-[var(--border)]">
          No appointments yet — when a caller books a slot on a live call it appears here, and deleting
          it frees the slot for future calls.
        </div>
      )}
      <div className="space-y-2">
        {bookings.map((b, i) => (
          <div key={`${b.conn_id}-${i}`} className="flex flex-wrap items-center gap-3 rounded-lg bg-[var(--surface-2)] px-3 py-2 ring-1 ring-[var(--border)]">
            <span className="text-sm font-semibold">{b.appointment_time}</span>
            <span className="text-xs text-soft">{b.caller_name || 'Unknown caller'}{b.caller_phone ? ` · ${b.caller_phone}` : ''}</span>
            <span className="ml-auto text-2xs text-dim">{b.booked_at ? fmtDateTime(b.booked_at) : ''}</span>
            <Button variant="ghost" size="sm" onClick={() => void onFree(i)} title="Free this slot — the agent will offer it again"><Trash2 size={13} /></Button>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ───────────────────────── YAML tab ───────────────────────── */
function YamlTab({ name, onSaved, onDirtyChange }: { name: string; onSaved: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const toast = useToast()
  const [text, setText] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [result, setResult] = useState<{ valid: boolean; error?: string; agent_id?: string } | null>(null)
  const [validatedText, setValidatedText] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const loadedTextRef = useRef('')

  useEffect(() => {
    let on = true
    getRawYaml(name).then((t) => { if (on) { setText(t); loadedTextRef.current = t; setLoaded(true) } }).catch((e) => toast(`Load failed: ${e.message || e}`, 'error'))
    return () => { on = false }
  }, [name])

  const edited = loaded && text !== loadedTextRef.current
  useEffect(() => { onDirtyChange?.(edited) }, [edited, onDirtyChange])

  const onValidate = async () => {
    try { setResult(await validateRaw(name, text)) } catch (e: any) { setResult({ valid: false, error: e.message || String(e) }) }
    setValidatedText(text)
  }
  const onSave = async () => {
    setSaving(true)
    try {
      const v = await validateRaw(name, text)
      setResult(v)
      setValidatedText(text)
      if (!v.valid) { toast('Fix validation errors before saving', 'error'); return }
      // If-Match: the current on-disk rev, so a concurrent Copilot edit 409s
      // instead of being silently clobbered.
      let rev: string | undefined
      try { rev = (await getAgent(name))?._rev } catch { /* legacy last-write-wins */ }
      await putRawYaml(name, text, rev)
      loadedTextRef.current = text
      toast('Saved — effective on the next call', 'success')
      onSaved()
    } catch (e: any) {
      if (String(e?.message || e).startsWith('409')) {
        toast('This agent changed since you loaded it (edited by the Copilot?). '
          + 'Reload the YAML tab and reapply your edit.', 'error')
      } else {
        toast(`Save failed: ${e.message || e}`, 'error')
      }
    }
    finally { setSaving(false) }
  }

  const stale = result !== null && text !== validatedText

  return (
    <Card className="p-5 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow min-w-0 max-w-full truncate">agents/{name}/agent.yaml</span>
        {edited && <Badge tone="amber" soft>edited</Badge>}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" onClick={onValidate} disabled={!loaded}><CheckCircle2 size={14} /> Validate</Button>
          <Button variant="primary" onClick={onSave} disabled={!loaded || saving}><Save size={14} /> {saving ? 'Saving…' : 'Save'}</Button>
        </div>
      </div>
      {result && (
        <div
          role={result.valid && !stale ? 'status' : 'alert'}
          className={cx('flex items-start gap-2 rounded-lg px-3 py-2 text-xs ring-1',
            stale ? 'bg-[var(--surface-2)] text-dim ring-[var(--border)]'
              : result.valid ? 'bg-mint-400/10 text-mint-700 ring-mint-400/20 dark:text-mint-300' : 'bg-rose-500/10 text-rose-600 ring-rose-500/20 dark:text-rose-300')}>
          {stale ? <AlertTriangle size={14} className="mt-0.5" /> : result.valid ? <CheckCircle2 size={14} className="mt-0.5" /> : <AlertTriangle size={14} className="mt-0.5" />}
          <span>{stale ? 'YAML changed since this check — validate again' : result.valid ? `Valid — ${result.agent_id}` : result.error}</span>
        </div>
      )}
      <Textarea
        rows={26}
        spellCheck={false}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); if (loaded && !saving) onSave() } }}
        className="font-mono text-xs leading-relaxed"
      />
      <p className="text-2xs text-dim">Tip: Ctrl/Cmd+S validates and saves.</p>
    </Card>
  )
}

/* ───────────────────────── Routing tab ───────────────────────── */
function RoutingTab({ name }: { name: string }) {
  const toast = useToast()
  const [r, setR] = useState<Routing>({ did: '', caller_id_prefixes: [], fallback_agent: '', time_windows: [] })
  const [win, setWin] = useState<TimeWindowDraft>({ start: '', end: '' })
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let on = true
    getRouting(name).then((data) => {
      if (!on) return
      setR({ did: data.did || '', caller_id_prefixes: data.caller_id_prefixes || [], fallback_agent: data.fallback_agent || '', time_windows: data.time_windows || [] })
      const w = (data.time_windows || [])[0]
      if (w) setWin({ start: String(w.start), end: String(w.end) })
      setLoaded(true)
    })
    return () => { on = false }
  }, [name])

  const onSave = async () => {
    const time_windows = win.start !== '' && win.end !== '' ? [{ start: Number(win.start), end: Number(win.end) }] : []
    const body: Routing = {
      did: r.did || '',
      caller_id_prefixes: r.caller_id_prefixes || [],
      fallback_agent: r.fallback_agent || '',
      time_windows,
    }
    setSaving(true)
    try { await putRouting(name, body); toast('Routing saved', 'success') }
    catch (e: any) { toast(`Save failed: ${e.message || e}`, 'error') }
    finally { setSaving(false) }
  }

  return (
    <Card className="p-5 space-y-4">
      <p className="text-xs text-dim">
        Routing decides which agent answers a given inbound number. The platform currently runs a single <span className="font-semibold text-soft">active</span> agent;
        these rules apply once per-DID routing is enabled.
      </p>
      <RoutingFields r={r} setR={setR} win={win} setWin={setWin} disabled={!loaded} />
      <div className="flex justify-end"><Button variant="primary" onClick={onSave} disabled={!loaded || saving}><Save size={14} /> {saving ? 'Saving…' : 'Save routing'}</Button></div>
    </Card>
  )
}

/* ───────────────────────── Documents tab ───────────────────────── */
/* Index state → what the user reads. `ok` is searchable by meaning; the rest
 * are keyword-only until a re-index succeeds, and the row's `detail` says why
 * (the provider's own sentence — "HTTP 429 …", "index built with X"). */
const INDEX_STATE: Record<string, { label: string; tone: 'mint' | 'amber' | 'rose' | 'neutral' }> = {
  ok: { label: 'Indexed', tone: 'mint' },
  stale: { label: 'Needs re-index', tone: 'amber' },
  unlabelled: { label: 'Needs re-index', tone: 'amber' },
  not_embedded: { label: 'Keyword search only', tone: 'rose' },
  unreadable: { label: 'Index unreadable', tone: 'rose' },
}

const fmtMB = (bytes: number) => `${(bytes / 1048576).toFixed(bytes < 1048576 ? 2 : 1)} MB`

function DocumentsTab({ name }: { name: string }) {
  const toast = useToast()
  const confirm = useConfirm()
  const { data, isLoading, error } = useGet<DocsList>(['docs', name], `/agents/${name}/documents`)
  const refresh = useRefresh()
  const [busy, setBusy] = useState(false)
  const [reindexing, setReindexing] = useState(false)
  const [extractingId, setExtractingId] = useState<string | null>(null)
  const [diff, setDiff] = useState<{ docId: string; fields: any; missing: MissingField[] } | null>(null)
  // No <form>: the design-system Button always renders type="button", so a
  // submit-driven upload never fired. The click is the upload.
  const [file, setFile] = useState<File | null>(null)
  const [orgName, setOrgName] = useState('')
  const [docType, setDocType] = useState('General')
  const [description, setDescription] = useState('')
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const clearFile = () => { setFile(null); if (fileRef.current) fileRef.current.value = '' }
  const pickFile = (f: File | null | undefined) => {
    if (!f) return
    if (!docSupported(f.name)) { toast(`${f.name}: unsupported type — upload ${DOC_TYPES_HUMAN}`, 'error'); clearFile(); return }
    if (f.size > DOC_MAX_MB * 1048576) { toast(`${f.name} is ${fmtMB(f.size)} — the limit is ${DOC_MAX_MB} MB`, 'error'); clearFile(); return }
    setFile(f)
  }

  const onUpload = async () => {
    if (!file) { toast('Choose a file first', 'error'); return }
    const form = new FormData()
    form.set('file', file)
    form.set('org_name', orgName.trim())
    form.set('doc_type', docType.trim() || 'General')
    form.set('description', description.trim())
    setBusy(true)
    try {
      const res = await uploadDoc(name, form)
      if (res.status === 'duplicate') {
        toast(`Already uploaded as "${res.meta?.filename || res.doc_id}" — nothing added`, 'warn')
      } else if (res.embedded) {
        toast(`Uploaded & indexed — ${res.meta?.chunk_count ?? 0} chunk${res.meta?.chunk_count === 1 ? '' : 's'}`, 'success')
      } else {
        toast(`Uploaded, but keyword search only — ${res.index_state?.detail || 'the vector index could not be built'}. Use Re-index to retry.`, 'warn')
      }
      refresh(['docs', name])
      clearFile(); setDescription('')
    } catch (e: any) { toast(`Upload failed: ${e.message || e}`, 'error') }
    finally { setBusy(false) }
  }
  const onReindex = async () => {
    setReindexing(true)
    try {
      const r = await reindexDocs(name)
      const failed = r.failed?.length ?? 0
      const n = r.reindexed?.length ?? 0
      toast(
        failed ? `Re-indexed ${n}, ${failed} failed — each document's status says why` : `Re-indexed ${n} document${n === 1 ? '' : 's'}`,
        failed ? 'warn' : 'success',
      )
      refresh(['docs', name])
    } catch (e: any) { toast(`Re-index failed: ${e.message || e}`, 'error') }
    finally { setReindexing(false) }
  }
  const onDelete = async (docId: string, filename?: string) => {
    const ok = await confirm({
      title: `Delete "${filename || docId}"?`,
      description: 'The document and its index are removed — the agent can no longer draw on it during calls.',
      destructive: true,
    })
    if (!ok) return
    try { await deleteDoc(name, docId); toast('Deleted', 'success'); refresh(['docs', name]) }
    catch (e: any) { toast(`Delete failed: ${e.message || e}`, 'error') }
  }
  const onExtract = async (docId: string) => {
    setBusy(true)
    setExtractingId(docId)
    try {
      const res = await extractYaml(name, docId)
      setDiff({ docId, fields: res.fields ?? {}, missing: res.missing ?? [] })
    }
    catch (e: any) { toast(`Extract failed: ${e.message || e}`, 'error') }
    finally { setBusy(false); setExtractingId(null) }
  }
  const onApply = async (fields: any) => {
    if (!diff) return
    setBusy(true)
    try { await applyYaml(name, diff.docId, fields); toast('Applied to agent.yaml', 'success'); setDiff(null); refresh(['agent', name]) }
    catch (e: any) { toast(`Apply failed: ${e.message || e}`, 'error') }
    finally { setBusy(false) }
  }

  const docs = data?.documents ?? []
  const index = data?.index
  const stateOf = (docId: string) => index?.documents.find((r) => r.doc_id === docId)
  const needs = index?.needs_reindex ?? []
  const noCredentials = index?.runtime?.available === false

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <div className="eyebrow mb-3">Upload knowledge document</div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div
            className={cx(
              'flex flex-wrap items-center gap-3 rounded-2xl border border-dashed border-[var(--ui-border)] p-4 transition sm:col-span-2',
              dragging && 'ring-2 ring-mint-400/40',
            )}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); if (!busy) pickFile(e.dataTransfer.files?.[0]) }}
          >
            <FileUp size={18} className="shrink-0 text-mint-500" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold text-[var(--text)]">
                {file ? file.name : 'Drop a document here, or choose a file'}
              </div>
              <div className="text-2xs text-dim">
                {file ? `${fmtMB(file.size)} · ready to upload` : `${DOC_TYPES_HUMAN} · up to ${DOC_MAX_MB} MB`}
              </div>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept={DOC_ACCEPT}
              className="hidden"
              onChange={(e) => pickFile(e.target.files?.[0])}
            />
            <Button variant="soft" size="sm" onClick={() => fileRef.current?.click()} disabled={busy}>Choose file</Button>
            {file && (
              <IconButton label={`Remove ${file.name}`} onClick={clearFile} disabled={busy}><X size={14} /></IconButton>
            )}
          </div>
          <Field label="Organisation name">
            <Input value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder="Organisation name" />
          </Field>
          <Field label="Document type">
            <Input value={docType} onChange={(e) => setDocType(e.target.value)} placeholder="Brochure, price list, FAQ…" />
          </Field>
          <Field label="Short description">
            <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What the agent should use this for" />
          </Field>
          <div className="flex items-end">
            <Button variant="primary" onClick={() => void onUpload()} disabled={busy || !file}>
              {busy && !extractingId ? <><Spinner className="h-3.5 w-3.5" /> Uploading & indexing…</> : <><Upload size={14} /> Upload</>}
            </Button>
          </div>
        </div>
        <p className="mt-2 text-2xs text-dim">
          Text is extracted, chunked and indexed so the agent can quote it on calls. "Extract fields" then proposes agent settings from the document.
        </p>
      </Card>

      {index && (noCredentials || needs.length > 0) && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl bg-amber-500/10 px-4 py-3 text-xs text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-300">
          <AlertTriangle size={14} className="shrink-0" />
          <span className="min-w-0 flex-1">
            {noCredentials
              ? `No embedding credentials configured (${index.runtime?.key_env || 'API key'}) — documents are keyword-searchable only.`
              : needs.length === 1
                ? '1 document is not in the vector index — the agent can only keyword-match it on calls.'
                : `${needs.length} documents are not in the vector index — the agent can only keyword-match them on calls.`}
          </span>
          {!noCredentials && (
            <Button size="sm" variant="soft" onClick={() => void onReindex()} disabled={reindexing || busy}>
              {reindexing ? <><Spinner className="h-3.5 w-3.5" /> Re-indexing…</> : <><RefreshCw size={13} /> Re-index {needs.length}</>}
            </Button>
          )}
        </div>
      )}

      <AsyncState loading={isLoading} error={error}>
        {docs.length === 0 ? (
          <EmptyState
            icon={<FileText size={20} />}
            title="No documents yet"
            hint="Upload a brochure, price list or FAQ — the agent quotes it on calls, and Extract fields turns it into agent settings."
          />
        ) : (
          <div className="space-y-2">
            {docs.map((doc) => {
              const st = stateOf(doc.doc_id)
              const badge = st ? (INDEX_STATE[st.state] ?? INDEX_STATE.not_embedded) : null
              const meta = [
                doc.doc_type || 'General',
                doc.org_name,
                doc.chunk_count ? `${doc.chunk_count} chunk${doc.chunk_count === 1 ? '' : 's'}` : '',
                doc.uploaded_at,
              ].filter(Boolean).join(' · ')
              return (
                <Card key={doc.doc_id} className="flex flex-wrap items-center gap-3 p-4">
                  <FileText size={16} className="shrink-0 text-indigo-400" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="truncate text-sm font-semibold text-[var(--text)]">{doc.filename || doc.doc_id}</span>
                      {badge && (
                        <span title={st?.state === 'ok' ? `${st.model || ''}${st.dim ? ` · ${st.dim} dimensions` : ''}` : st?.detail || ''}>
                          <Badge tone={badge.tone} soft>{badge.label}</Badge>
                        </span>
                      )}
                    </div>
                    <div className="text-2xs text-dim">{meta}</div>
                    {st && st.state !== 'ok' && st.detail && (
                      <div className="mt-1 text-2xs text-amber-700 dark:text-amber-300">{st.detail}</div>
                    )}
                  </div>
                  <div className="ml-auto flex items-center gap-1">
                    <Button variant="ghost" size="sm" onClick={() => void onExtract(doc.doc_id)} disabled={busy}>
                      {extractingId === doc.doc_id
                        ? <><Spinner className="h-3.5 w-3.5" /> Extracting…</>
                        : <><Sparkles size={14} /> Extract fields</>}
                    </Button>
                    <IconButton label={`Delete ${doc.filename || 'document'}`} onClick={() => void onDelete(doc.doc_id, doc.filename)} disabled={busy}>
                      <Trash2 size={14} />
                    </IconButton>
                  </div>
                </Card>
              )
            })}
          </div>
        )}
      </AsyncState>
      {index?.runtime?.model && docs.length > 0 && (
        <p className="text-2xs text-dim">
          Vector index: {index.runtime.model}{index.runtime.dim ? ` · ${index.runtime.dim} dimensions` : ''}.
        </p>
      )}

      {diff && (
        <ExtractReview
          fields={diff.fields}
          missing={diff.missing}
          busy={busy}
          onCancel={() => setDiff(null)}
          onApply={onApply}
        />
      )}
    </div>
  )
}

/* ─────────────────── extraction review modal ───────────────────
 * Editable per-field review of what the document yielded. Fields the
 * document did NOT contain are shown highlighted with an explicit ask, so
 * the user fills the gaps before applying. Empty values are dropped from
 * the applied payload (they never blank out existing YAML values). */
const EXTRACT_LABELS: Record<string, string> = {
  agent_name: 'Agent name', company_name: 'Company name', company_tagline: 'Company tagline',
  role_description: 'Agent role', contact: 'Contact number', call_direction: 'Call type',
  opening_greeting: 'Opening greeting', portfolio_prompt_text: 'Products / services',
  locations: 'Locations served', objection_patterns: 'Objection phrases',
  appointment_slots: 'Calendar slots', known_projects: 'Known products (JSON)',
  extra_always_rules: 'Always rules', extra_never_rules: 'Never rules',
}
const COMMA_KEYS = new Set(['locations', 'objection_patterns'])
const LINES_KEYS = new Set(['extra_always_rules', 'extra_never_rules'])
const LONG_KEYS = new Set(['opening_greeting', 'portfolio_prompt_text'])

function toDraft(key: string, val: any): string {
  if (val == null) return ''
  if (COMMA_KEYS.has(key)) return (val as any[]).join(', ')
  if (LINES_KEYS.has(key)) return (val as any[]).join('\n')
  if (key === 'appointment_slots')
    return (val as any[]).map((s) => `${s?.day ?? ''} at ${s?.time ?? ''}`).join('\n')
  if (typeof val === 'object') return JSON.stringify(val, null, 2)
  return String(val)
}

function fromDraft(key: string, text: string): any | undefined {
  const t = text.trim()
  if (!t) return undefined
  if (COMMA_KEYS.has(key)) return t.split(',').map((s) => s.trim()).filter(Boolean)
  if (LINES_KEYS.has(key)) return t.split('\n').map((s) => s.trim()).filter(Boolean)
  if (key === 'appointment_slots') {
    const slots = t.split('\n').map((line) => {
      const [day, ...rest] = line.split(' at ')
      return { day: (day || '').trim(), time: rest.join(' at ').trim() }
    }).filter((s) => s.day && s.time)
    return slots.length ? slots : undefined
  }
  if (key === 'known_projects') { try { return JSON.parse(t) } catch { return undefined } }
  return t
}

function ExtractReview({ fields, missing, busy, onCancel, onApply }: {
  fields: Record<string, any>
  missing: MissingField[]
  busy: boolean
  onCancel: () => void
  onApply: (fields: Record<string, any>) => void
}) {
  const confirm = useConfirm()
  const missingKeys = new Set(missing.map((m) => m.key))
  const orderedKeys = [
    ...Object.keys(EXTRACT_LABELS).filter((k) => k in fields || missingKeys.has(k)),
    ...Object.keys(fields).filter((k) => !(k in EXTRACT_LABELS)),
  ]
  const [draft, setDraft] = useState<Record<string, string>>(() => {
    const d: Record<string, string> = {}
    orderedKeys.forEach((k) => { d[k] = toDraft(k, fields[k]) })
    return d
  })
  // Snapshot of the initial draft so dismissal can warn about typed-in edits.
  const initialRef = useRef<Record<string, string> | null>(null)
  if (initialRef.current === null) initialRef.current = { ...draft }
  const changed = orderedKeys.some((k) => (draft[k] ?? '') !== (initialRef.current?.[k] ?? ''))
  const firstMissingKey = missing.find((m) => !(draft[m.key] ?? '').trim())?.key

  const requestCancel = async () => {
    if (changed) {
      const ok = await confirm({
        title: 'Discard extracted fields?',
        description: 'The extracted fields and the edits you typed here will be lost.',
        confirmLabel: 'Discard',
        destructive: true,
      })
      if (!ok) return
    }
    onCancel()
  }

  const apply = () => {
    const out: Record<string, any> = {}
    orderedKeys.forEach((k) => {
      const v = fromDraft(k, draft[k] ?? '')
      if (v !== undefined) out[k] = v
    })
    onApply(out)
  }

  const stillEmpty = missing.filter((m) => !(draft[m.key] ?? '').trim())

  return (
    <Dialog open onOpenChange={(open) => { if (!open) void requestCancel() }}>
      <DialogContent
        aria-describedby={undefined}
        className="card flex max-h-[85vh] w-full max-w-2xl flex-col gap-0 overflow-hidden border-0 p-0"
      >
        <DialogHeader className="border-b border-[var(--border)] px-4 py-4 pr-12 text-left sm:px-6">
          <DialogTitle className="flex items-center gap-2 font-display text-base font-bold tracking-tight">
            <Sparkles size={16} className="text-mint-500" /> Review extracted fields
          </DialogTitle>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto px-4 py-4 sm:px-6">
          {stillEmpty.length > 0 && (
            <div className="mb-3 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-300">
              <span className="font-semibold">The document didn't contain:</span>{' '}
              {stillEmpty.map((m) => m.label).join(', ')} — please fill them in below (or apply without them and
              set them later in Settings).
            </div>
          )}
          <div className="space-y-3">
            {orderedKeys.map((k) => {
              const isMissing = missingKeys.has(k) && !(draft[k] ?? '').trim()
              const hint = missing.find((m) => m.key === k)?.hint
              const label = EXTRACT_LABELS[k] ?? k
              return (
                <div key={k} className={cx(isMissing && 'rounded-lg bg-amber-500/5 p-2 ring-1 ring-amber-500/20')}>
                  <Field label={isMissing ? `${label} — needed` : label}>
                    {k === 'call_direction' ? (
                      <Select value={draft[k] ?? ''} onChange={(e) => setDraft((p) => ({ ...p, [k]: e.target.value }))}>
                        <option value="">(not set)</option>
                        <option value="inbound">inbound — answers calls</option>
                        <option value="outbound">outbound — places calls</option>
                      </Select>
                    ) : LONG_KEYS.has(k) || LINES_KEYS.has(k) || k === 'appointment_slots' || k === 'known_projects' || (draft[k] ?? '').includes('\n') ? (
                      <Textarea
                        rows={k === 'portfolio_prompt_text' ? 6 : 3}
                        value={draft[k] ?? ''}
                        placeholder={hint}
                        autoFocus={k === firstMissingKey}
                        onChange={(e) => setDraft((p) => ({ ...p, [k]: e.target.value }))}
                      />
                    ) : (
                      <Input
                        value={draft[k] ?? ''}
                        placeholder={hint}
                        autoFocus={k === firstMissingKey}
                        onChange={(e) => setDraft((p) => ({ ...p, [k]: e.target.value }))}
                      />
                    )}
                  </Field>
                  {k === 'appointment_slots' && <span className="mt-1 block text-2xs text-dim">One slot per line, e.g. "Tomorrow at 10:00 AM"</span>}
                </div>
              )
            })}
          </div>
        </div>
        <DialogFooter className="gap-2 border-t border-[var(--border)] px-4 py-3 sm:px-6">
          <Button variant="ghost" onClick={() => void requestCancel()}>Cancel</Button>
          <Button variant="primary" onClick={apply} disabled={busy}>Apply to agent.yaml</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ───────────────────────── Voice tab ───────────────────────── */
function VoiceTab({ agent }: { agent: any }) {
  const toast = useToast()
  const voices: Record<string, { model: string; speaker: string; pace?: number }> = agent?.languages?.tts_voices ?? {}
  const entries = Object.entries(voices)
  const [playingLang, setPlayingLang] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const play = (lang: string, v: { model: string; speaker: string; pace?: number }) => {
    try {
      audioRef.current?.pause()
      const audio = new Audio(ttsPreviewUrl(v.speaker, v.model, BCP47[lang] || 'en-IN', v.pace))
      audioRef.current = audio
      setPlayingLang(lang)
      audio.onended = () => setPlayingLang(null)
      audio.onerror = () => { setPlayingLang(null); toast('Preview failed to load', 'error') }
      audio.play().catch((e: any) => { setPlayingLang(null); toast(`Preview failed: ${e.message || e}`, 'error') })
    } catch (e: any) { setPlayingLang(null); toast(`Preview failed: ${e.message || e}`, 'error') }
  }

  if (entries.length === 0) return <EmptyState icon={<Volume2 size={20} />} title="No voices configured" hint="Add tts_voices in the YAML definition." />
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {entries.map(([lang, v]) => (
        <Card key={lang} className="flex items-center gap-3 p-4">
          <span className={cx('grid h-10 w-10 place-items-center rounded-xl bg-[var(--surface-2)] ring-1 ring-[var(--border)]',
            playingLang === lang ? 'animate-pulse text-mint-500' : 'text-indigo-400')}><Volume2 size={18} /></span>
          <div className="min-w-0">
            <div className="text-sm font-semibold text-[var(--text)]">{lang.toUpperCase()}</div>
            <div className="truncate text-2xs text-dim">{v.model} · {v.speaker}{v.pace ? ` · ${v.pace}× pace` : ''}</div>
          </div>
          <Button variant="ghost" className="ml-auto" onClick={() => play(lang, v)}>
            {playingLang === lang ? <><Spinner className="h-3.5 w-3.5" /> Playing…</> : <><Volume2 size={14} /> Preview</>}
          </Button>
        </Card>
      ))}
    </div>
  )
}

/* ───────────────────────── Stages tab (v2 surgical ops) ─────────────────────
 * Visual stage list wired to PATCH /api/agents/{name}/ops — the SAME surgical
 * machinery the Copilot uses, so both paths converge on the server store
 * (revisioned, concurrency-safe, scope-proven). */
type StageRow = {
  id: string
  goal: string
  turns_until_advance?: number
  role?: string
  [k: string]: any
}

function StagesTab({ name, onSaved }: { name: string; onSaved: () => void }) {
  const toast = useToast()
  const confirm = useConfirm()
  const prompt = usePrompt()
  const [stages, setStages] = useState<StageRow[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)
  const [goalDraft, setGoalDraft] = useState('')

  const load = () => {
    getAgent(name)
      .then((def) => setStages(def?.call_flow?.stages ?? []))
      .catch((e) => toast(`Load failed: ${e.message || e}`, 'error'))
  }
  useEffect(load, [name]) // eslint-disable-line react-hooks/exhaustive-deps

  const runOps = async (ops: AgentOp[], what: string) => {
    setBusy(true)
    try {
      const res = await patchAgentOps(name, ops)
      toast(`${what} — revision ${res.revision}, effective on the next call`, 'success')
      onSaved()
      load()
    } catch (e: any) {
      if (String(e?.message || e).startsWith('409')) {
        toast('The agent changed underneath this view — reloading.', 'error')
        load()
      } else {
        toast(`${what} failed: ${e.message || e}`, 'error')
      }
    } finally {
      setBusy(false)
      setEditing(null)
    }
  }

  const move = (i: number, dir: -1 | 1) => {
    const to = i + dir
    if (!stages || to < 0 || to >= stages.length) return
    void runOps(
      [{ op: 'move', path: `call_flow.stages[${i}]`, to },
       // renumber order ints server-side too (set both swapped orders)
       { op: 'set', path: `call_flow.stages[${to}].order`, value: to },
       { op: 'set', path: `call_flow.stages[${i}].order`, value: i }],
      `Moved ${stages[i].id}`)
  }

  const remove = async (i: number) => {
    if (!stages || stages.length <= 1) return
    const ok = await confirm({
      title: `Remove stage ${stages[i].id}?`,
      description: 'The stage is removed from the call flow. Every edit is versioned, so it can be restored from History.',
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (!ok) return
    const renumber: AgentOp[] = stages
      .filter((_, j) => j !== i)
      .map((_, j) => ({ op: 'set' as const, path: `call_flow.stages[${j}].order`, value: j }))
    void runOps(
      [{ op: 'remove_at', path: `call_flow.stages[${i}]` }, ...renumber],
      `Removed ${stages[i].id}`)
  }

  const saveGoal = (i: number) => {
    if (!stages) return
    void runOps(
      [{ op: 'set', path: `call_flow.stages[${i}].goal`, value: goalDraft }],
      `Updated ${stages[i].id}`)
  }

  const addStage = async () => {
    if (!stages) return
    const raw = await prompt({
      title: 'New stage id',
      description: 'Stage ids are uppercased, e.g. HANDLE_REJECTION.',
      placeholder: 'e.g. QUALIFY',
      validate: (v) =>
        /^[A-Za-z][A-Za-z0-9_-]*$/.test(v.trim())
          ? null
          : 'Letters, numbers, _ and - only; must start with a letter',
    })
    if (raw == null) return
    const id = raw.trim().toUpperCase().replace(/\s+/g, '_')
    if (!id) return
    if (stages.some((s) => s.id === id)) { toast(`Stage ${id} already exists`, 'error'); return }
    void runOps(
      [{ op: 'append', path: 'call_flow.stages',
         value: { id, order: stages.length, goal: 'Describe what this stage must accomplish.', turns_until_advance: 8 } }],
      `Added ${id}`)
  }

  return (
    <Card className="space-y-3 p-5">
      <div className="flex items-center gap-2">
        <span className="eyebrow">Call stages — surgical edits, versioned</span>
        <div className="ml-auto">
          <Button variant="ghost" size="sm" disabled={busy || !stages} onClick={() => void addStage()}>
            <Plus size={13} /> Add stage
          </Button>
        </div>
      </div>
      {!stages ? <TabSkeleton /> : stages.length === 0 ? (
        <EmptyState title="No stages" hint="Add the first stage of the call flow." />
      ) : (
        <div className="space-y-2">
          {stages.map((s, i) => (
            <div key={`${s.id}-${i}`} className="rounded-xl bg-[var(--surface-2)] p-3 ring-1 ring-[var(--border)]">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone="mint" soft>{i + 1}</Badge>
                <span className="font-mono text-sm font-semibold">{s.id}</span>
                {s.role && <Badge soft>{s.role}</Badge>}
                {s.turns_until_advance != null && (
                  <span className="text-2xs text-dim">advance after {s.turns_until_advance} turns</span>
                )}
                <div className="ml-auto flex items-center gap-1">
                  <Button variant="ghost" size="sm" disabled={busy || i === 0} onClick={() => move(i, -1)} title="Move up"><ArrowUp size={13} /></Button>
                  <Button variant="ghost" size="sm" disabled={busy || i === stages.length - 1} onClick={() => move(i, 1)} title="Move down"><ArrowDown size={13} /></Button>
                  <Button variant="ghost" size="sm" disabled={busy}
                    onClick={() => { setEditing(editing === i ? null : i); setGoalDraft(s.goal || '') }}
                    title="Edit the stage goal">
                    <Code2 size={13} />
                  </Button>
                  <Button variant="ghost" size="sm" disabled={busy || stages.length <= 1} onClick={() => void remove(i)} title="Remove stage"><Trash2 size={13} /></Button>
                </div>
              </div>
              {editing === i ? (
                <div className="mt-2 space-y-2">
                  <Textarea rows={3} value={goalDraft} onChange={(e) => setGoalDraft(e.target.value)} className="text-sm" />
                  <div className="flex gap-2">
                    <Button size="sm" disabled={busy || !goalDraft.trim()} onClick={() => saveGoal(i)}><Save size={13} /> Save goal</Button>
                    <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(null)}>Cancel</Button>
                  </div>
                </div>
              ) : (
                <p className="mt-1.5 text-xs leading-relaxed text-soft">{s.goal}</p>
              )}
            </div>
          ))}
        </div>
      )}
      <p className="text-2xs text-dim">
        Each edit lands as a single surgical operation — versioned in History and provably touching nothing else.
      </p>
    </Card>
  )
}

/* ───────────────────────── History tab (v2 revisions) ───────────────────── */
function HistoryTab({ name, onChanged }: { name: string; onChanged: () => void }) {
  const toast = useToast()
  const confirm = useConfirm()
  const [revs, setRevs] = useState<AgentRevision[] | null>(null)
  const [busy, setBusy] = useState(false)

  const load = () => {
    listRevisions(name)
      .then((r) => setRevs(r.revisions ?? []))
      .catch((e) => toast(`Load failed: ${e.message || e}`, 'error'))
  }
  useEffect(load, [name]) // eslint-disable-line react-hooks/exhaustive-deps

  const onRollback = async (rev: AgentRevision) => {
    const ok = await confirm({
      title: 'Restore this snapshot?',
      description: `The agent is rolled back to how it was BEFORE "${rev.reason || rev.actor || `revision ${rev.rev_id}`}". Restoring is itself snapshotted, so it can be undone.`,
      confirmLabel: 'Restore',
      destructive: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      const res = await rollbackAgent(name, rev.rev_id)
      toast(`Rolled back — now revision ${res.revision}, effective on the next call`, 'success')
      onChanged()
      load()
    } catch (e: any) {
      toast(`Rollback failed: ${e.message || e}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="space-y-3 p-5">
      <span className="eyebrow">Edit history — every change is reversible</span>
      {!revs ? <TabSkeleton /> : revs.length === 0 ? (
        <EmptyState
          title="No revisions yet"
          hint="Every save (Copilot, Settings, YAML, Stages) snapshots the agent here first."
        />
      ) : (
        <div className="space-y-2">
          {revs.map((r) => (
            <div key={r.rev_id} className="flex flex-wrap items-center gap-3 rounded-lg bg-[var(--surface-2)] px-3 py-2 ring-1 ring-[var(--border)]">
              <Badge soft>#{r.rev_id}</Badge>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold">{r.reason || 'edit'}</div>
                <div className="text-2xs text-dim">
                  {r.actor || 'unknown'} · {fmtDateTime(String(r.created_at))}
                </div>
              </div>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => void onRollback(r)}
                title="Restore the agent to the state saved in this snapshot">
                <RotateCcw size={13} /> Restore
              </Button>
            </div>
          ))}
        </div>
      )}
      <p className="text-2xs text-dim">
        Snapshots are taken BEFORE each write (last 50 kept). Restoring is itself snapshotted, so a rollback can be undone.
      </p>
    </Card>
  )
}
