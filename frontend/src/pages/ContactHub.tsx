/**
 * Customer Intelligence Hub — one contact, the whole relationship:
 * AI profile digest + score trends, unified timeline (calls, missed dials,
 * follow-ups, commitments, memory updates, notes), per-contact call list,
 * persistent memory panel, upcoming follow-ups, recommended next actions,
 * lifetime stats, owner-managed sharing (grants).
 * Backed by /api/intel/contacts/{id}(/timeline|/calls|/grants).
 */
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  BrainCircuit, CalendarClock, Handshake, History, ListTodo,
  Pencil, Phone, PhoneIncoming, PhoneMissed, PhoneOutgoing, Share2, Sparkles,
  StickyNote, UserRound, X, Zap,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { api } from '../api/client'
import { post, patch, del } from '../api/mutate'
import type { ContactEvent, ContactHubPayload, IntelCallRow } from '../api/intel'
import { Badge, Button, Card, EmptyState, IconButton, SectionHead, Spinner, cx } from '../components/ui'
import {
  AsyncState, PageHeader, StatusBadge, Table, THead, TH, TR, TD, Field, Input, Select,
} from '../components/kit'
import {
  Chips, CommitmentCard, FilterChips, ScoreRingRow, StatTile, TimelineList,
  type TimelineEvent,
} from '../components/insight'
import { Sparkline } from '../components/charts'
import { CHART } from '../lib/chartColors'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toast'
import { dur, fmtDateTime, fmtDayLabel } from '../lib/format'

const KIND_FILTERS: { label: string; value: string }[] = [
  { label: 'All', value: '' },
  { label: 'Calls', value: 'call.inbound,call.outbound' },
  { label: 'Missed', value: 'call.missed,dial.attempted' },
  { label: 'Follow-ups', value: 'followup.created,followup.completed' },
  { label: 'Promises', value: 'commitment.made' },
  { label: 'Memory', value: 'memory.updated,call.analyzed' },
  { label: 'Notes', value: 'note.added,contact.updated' },
]

function kindIcon(kind: string) {
  if (kind === 'call.inbound') return <PhoneIncoming size={13} />
  if (kind === 'call.outbound') return <PhoneOutgoing size={13} />
  if (kind === 'call.missed' || kind === 'dial.attempted') return <PhoneMissed size={13} />
  if (kind.startsWith('followup')) return <CalendarClock size={13} />
  if (kind === 'commitment.made') return <Handshake size={13} />
  if (kind === 'memory.updated') return <UserRound size={13} />
  if (kind === 'call.analyzed') return <BrainCircuit size={13} />
  if (kind === 'automation.fired') return <Zap size={13} />
  return <StickyNote size={13} />
}

function kindTone(kind: string): 'mint' | 'indigo' | 'amber' | 'rose' | 'neutral' {
  if (kind === 'call.missed') return 'rose'
  if (kind.startsWith('call.')) return 'mint'
  if (kind.startsWith('followup') || kind === 'commitment.made') return 'amber'
  if (kind === 'memory.updated') return 'indigo'
  return 'neutral'
}

function eventTitle(e: ContactEvent): string {
  const p = e.payload ?? {}
  switch (e.kind) {
    case 'call.inbound': return 'Inbound call' + (p.duration_s ? ` · ${dur(p.duration_s)}` : '')
    case 'call.outbound': return 'Outbound call' + (p.duration_s ? ` · ${dur(p.duration_s)}` : '')
    case 'call.missed': return `Missed — ${String(p.outcome ?? 'failed').replace(/_/g, ' ')}`
    case 'dial.attempted': return 'Dial placed'
    case 'call.analyzed': return `AI analysis · lead ${p.lead_score ?? '—'}`
    case 'followup.created': return `Follow-up scheduled${p.scheduled_at ? ` · ${fmtDateTime(String(p.scheduled_at))}` : ''}`
    case 'followup.completed': return 'Follow-up completed'
    case 'commitment.made': return `Promise (${p.made_by ?? '—'})`
    case 'memory.updated': return `Memory updated · v${p.version ?? '—'}`
    case 'note.added': return 'Note updated'
    case 'contact.updated': return `Contact edited${p.fields ? ` · ${(p.fields as string[]).join(', ')}` : ''}`
    case 'automation.fired': return `Automation fired · ${p.rule ?? ''}`
    default: return e.kind
  }
}

function eventDetail(e: ContactEvent): string {
  const p = e.payload ?? {}
  return p.summary ?? p.description ?? p.customer_intent ?? p.reason
    ?? (p.retry?.note ? `Retry: ${p.retry.note}` : '')
}

function factTexts(list: any[] | undefined, n = 6): string[] {
  return (list ?? [])
    .filter((f) => (f?.status ?? 'active') === 'active')
    .slice(-n).map((f) => f.text)
}

const LANG_NAMES: Record<string, string> = { en: 'English', hi: 'Hindi', gu: 'Gujarati' }

export default function ContactHub() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const toast = useToast()
  const refresh = useRefresh()
  const hubQ = useGet<ContactHubPayload>(['contact-hub', id], `/intel/contacts/${id}`)
  const [kinds, setKinds] = useState('')
  const timelineQ = useGet<{ items: ContactEvent[]; next_before: number | null }>(
    ['contact-timeline', id, kinds],
    `/intel/contacts/${id}/timeline?limit=30${kinds ? `&kinds=${kinds}` : ''}`)
  const callsQ = useGet<{ items: IntelCallRow[]; total: number }>(
    ['contact-calls', id], `/intel/contacts/${id}/calls?limit=25`)
  const [older, setOlder] = useState<ContactEvent[]>([])
  const [moreCursor, setMoreCursor] = useState<number | null | undefined>(undefined)
  const [loadingMore, setLoadingMore] = useState(false)
  const [noteOpen, setNoteOpen] = useState(false)
  const [note, setNote] = useState('')
  const [dialing, setDialing] = useState(false)

  // Sharing (owner-only): grants form state.
  const [grantEmail, setGrantEmail] = useState('')
  const [grantAccess, setGrantAccess] = useState<'read' | 'edit'>('read')
  const [granting, setGranting] = useState(false)

  // Navigating contact → contact reuses this component: without a reset the
  // previous person's "load older" pages and kind filter carry over into the
  // new one's timeline.
  useEffect(() => {
    setKinds('')
    setOlder([])
    setMoreCursor(undefined)
  }, [id])

  const d = hubQ.data
  const events = useMemo(() => {
    const first = timelineQ.data?.items ?? []
    const seen = new Set(first.map((e) => e.id))
    return [...first, ...older.filter((e) => !seen.has(e.id))]
  }, [timelineQ.data, older])
  // Cursor: the first page's next_before until "Load older" has run, then
  // whatever the last fetch reported. null = no more events.
  const cursor = moreCursor === undefined ? timelineQ.data?.next_before : moreCursor
  const hasMore = cursor != null

  async function loadMore() {
    if (cursor == null) return
    setLoadingMore(true)
    try {
      const r = await api<{ items: ContactEvent[]; next_before: number | null }>(
        `/intel/contacts/${id}/timeline?limit=30&before=${cursor}${kinds ? `&kinds=${kinds}` : ''}`)
      setOlder((o) => [...o, ...r.items])
      setMoreCursor(r.next_before)
    } catch (e: any) {
      toast(`Could not load older events: ${e?.message ?? e}`, 'error')
    } finally {
      setLoadingMore(false)
    }
  }

  const contact = d?.contact
  const uiHandle = contact ? (contact.legacy_id || `db${contact.id}`) : ''

  async function dial() {
    if (!uiHandle || dialing) return
    setDialing(true)
    try {
      await post(`/outbound/dial/${uiHandle}`, {})
      toast('Dialing — the agent will call this contact now', 'success')
    } catch (e: any) {
      toast(`Dial failed: ${e?.message ?? e}`, 'error')
    } finally {
      setDialing(false)
    }
  }

  async function saveNote() {
    if (!note.trim() || !uiHandle) return
    try {
      const existing = contact?.notes ? `${contact.notes}\n` : ''
      await patch(`/outbound/contacts/${uiHandle}`, { notes: `${existing}${note.trim()}` })
      toast('Note saved', 'success')
      setNoteOpen(false)
      setNote('')
      refresh(['contact-hub', id], ['contact-timeline', id])
    } catch (e: any) {
      toast(`Save failed: ${e?.message ?? e}`, 'error')
    }
  }

  async function addGrant() {
    const email = grantEmail.trim().toLowerCase()
    if (!email.includes('@') || granting) return
    setGranting(true)
    try {
      await post(`/intel/contacts/${id}/grants`, { grantee_email: email, access: grantAccess })
      toast(`Shared with ${email} (${grantAccess})`, 'success')
      setGrantEmail('')
      refresh(['contact-hub', id], ['contact-timeline', id])
    } catch (e: any) {
      toast(`Share failed: ${e?.message ?? e}`, 'error')
    } finally {
      setGranting(false)
    }
  }

  async function revokeGrant(g: { id: number; grantee_email: string }) {
    try {
      await del(`/intel/contacts/${id}/grants/${g.id}`)
      toast(`Access revoked for ${g.grantee_email}`, 'success')
      refresh(['contact-hub', id], ['contact-timeline', id])
    } catch (e: any) {
      toast(`Revoke failed: ${e?.message ?? e}`, 'error')
    }
  }

  const mem = d?.memory ?? {}
  const trend = d?.scores?.trend ?? []
  const latest = d?.scores?.latest
  const supersededCount = useMemo(() =>
    ['preferences', 'promises', 'objections'].reduce((n, k) =>
      n + ((mem as any)[k] ?? []).filter((f: any) => f.status && f.status !== 'active').length, 0),
    [mem])
  const rec = d?.recommendations ?? {}
  const outcomeMix = d?.stats?.outcome_mix ?? {}
  const grants = d?.grants ?? []

  const timelineEvents: TimelineEvent[] = events.map((e) => ({
    id: e.id,
    title: eventTitle(e),
    icon: kindIcon(e.kind),
    tone: e.kind === 'call.missed' ? 'rose' : undefined,
    meta: (
      <>
        <Badge tone={kindTone(e.kind)} soft>{e.kind}</Badge>
        <span className="text-2xs text-dim">
          {fmtDateTime(e.occurred_at)}
        </span>
      </>
    ),
    detail: eventDetail(e) || undefined,
    raw: e,
  }))

  const subLine = [
    contact?.phone,
    contact?.gender,
    mem.preferred_language
      ? `prefers ${LANG_NAMES[mem.preferred_language] ?? mem.preferred_language}` : '',
    mem.preferred_call_time ? `best time: ${mem.preferred_call_time}` : '',
    d?.access && d.access !== 'owner' ? `shared (${d.access})` : '',
  ].filter(Boolean).join(' · ')

  return (
    <div className="space-y-6">
      <PageHeader detail
        crumbs={[{ label: 'Contacts', to: '/contacts' }]}
        title={contact?.name || contact?.phone || `Contact #${id}`}
        sub={subLine || undefined}
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={() => setNoteOpen(true)} disabled={!contact}>
              <Pencil size={14} /> Add note
            </Button>
            <Button size="sm" onClick={dial} disabled={!contact || dialing}>
              {dialing ? <Spinner className="h-4 w-4" /> : <Phone size={14} />} Call
            </Button>
          </>
        }
      />

      <AsyncState loading={hubQ.isLoading} error={hubQ.error} empty={!d} variant="text" rows={8}>
        <div className="grid gap-6 xl:grid-cols-3">
          {/* ---------------- left 2/3: profile, timeline, calls */}
          <div className="space-y-6 xl:col-span-2">
            <Card className="p-5">
              <SectionHead title="AI customer profile"
                right={<Sparkles size={15} className="text-dim" />} />
              {d?.memory_meta?.digest ? (
                <p className="text-sm leading-relaxed text-soft">{d.memory_meta.digest}</p>
              ) : (
                <p className="text-sm text-dim">
                  No conversations analyzed yet — the profile builds automatically after the first call.
                </p>
              )}
              {d?.latest_analysis?.summary && (
                <p className="mt-2 text-xs text-dim">
                  Last call: {d.latest_analysis.summary}
                </p>
              )}
              <ScoreRingRow items={[
                { label: 'Lead', value: latest?.lead_score },
                { label: 'Opportunity', value: latest?.opportunity_score },
                { label: 'Risk', value: latest?.risk_score, invert: true },
              ]}>
                {trend.length >= 2 && (
                  <div className="min-w-[140px] max-w-[240px] flex-1">
                    <div className="eyebrow mb-1">Lead trend · {trend.length} calls</div>
                    <Sparkline data={trend.map((t) => t.lead_score ?? 0)} color={CHART.mint} height={36} />
                  </div>
                )}
                <div className="space-y-1 text-xs">
                  {latest?.sentiment && <div>Sentiment: <StatusBadge value={latest.sentiment} /></div>}
                  {mem.risk_level && <div>Churn risk: <StatusBadge value={mem.risk_level} /></div>}
                  {mem.last_intent && <div className="max-w-[220px] truncate" title={mem.last_intent}>Intent: <span className="text-soft">{mem.last_intent}</span></div>}
                </div>
              </ScoreRingRow>

              {/* Sharing — owner-only. Grants are read/edit; revoke is immediate. */}
              {d?.access === 'owner' && (
                <div className="mt-5 border-t border-[var(--border)] pt-4">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="eyebrow uppercase">Access</span>
                    <Share2 size={14} className="text-dim" />
                  </div>
                  {grants.length === 0 ? (
                    <p className="mb-2 text-xs text-dim">
                      Only you can see this contact. Share it with a teammate by email.
                    </p>
                  ) : (
                    <ul className="mb-3 space-y-1.5">
                      {grants.map((g) => (
                        <li key={g.id} className="flex items-center gap-2 text-xs">
                          <span className="min-w-0 flex-1 truncate text-soft">{g.grantee_email}</span>
                          <Badge tone={g.access === 'edit' ? 'indigo' : 'neutral'}>{g.access}</Badge>
                          <IconButton label={`Revoke access for ${g.grantee_email}`}
                            onClick={() => revokeGrant(g)} className="h-7 w-7">
                            <X size={13} />
                          </IconButton>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="flex flex-wrap items-center gap-2">
                    <Input
                      value={grantEmail}
                      onChange={(e) => setGrantEmail(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') addGrant() }}
                      type="email" inputMode="email"
                      placeholder="teammate@company.com"
                      aria-label="Teammate email"
                      className="w-auto min-w-0 flex-1"
                    />
                    <Select value={grantAccess} aria-label="Access level"
                      onChange={(e) => setGrantAccess(e.target.value as 'read' | 'edit')}>
                      <option value="read">Read</option>
                      <option value="edit">Edit</option>
                    </Select>
                    <Button size="sm" onClick={addGrant}
                      disabled={granting || !grantEmail.includes('@')}>
                      {granting ? <Spinner className="h-4 w-4" /> : <Share2 size={14} />} Grant
                    </Button>
                  </div>
                </div>
              )}
            </Card>

            <Card className="p-5">
              <SectionHead title="Timeline" sub="Every interaction, automatically recorded"
                right={<History size={15} className="text-dim" />} />
              <div className="mb-3">
                <FilterChips options={KIND_FILTERS} value={kinds}
                  onChange={(v) => { setKinds(v); setOlder([]); setMoreCursor(undefined) }} />
              </div>
              {timelineQ.isLoading ? (
                <div className="space-y-2 py-2">
                  {[0, 1, 2].map((i) => <div key={i} className="skeleton h-11" />)}
                </div>
              ) : events.length === 0 ? (
                <EmptyState icon={<History size={20} />} title="No events yet"
                  hint="Calls, follow-ups and memory updates appear here automatically." />
              ) : (
                <>
                  <TimelineList events={timelineEvents}
                    renderDetail={(item) => {
                      const e = item.raw as ContactEvent
                      const callKey = e.payload?.call_key
                      return (
                        <>
                          {callKey && (
                            <button onClick={() => nav(`/intelligence/${callKey}`)}
                              className="mt-0.5 text-xs font-medium text-mint-600 hover:underline dark:text-mint-300">
                              Open call workspace →
                            </button>
                          )}
                          {e.kind.startsWith('followup') && (
                            <button onClick={() => nav('/followups')}
                              className="mt-0.5 text-xs font-medium text-mint-600 hover:underline dark:text-mint-300">
                              Manage follow-ups →
                            </button>
                          )}
                        </>
                      )
                    }} />
                  {hasMore && (
                    <div className="mt-4 text-center">
                      <Button variant="soft" size="sm" onClick={loadMore} disabled={loadingMore}>
                        {loadingMore ? <Spinner className="h-4 w-4" /> : null} Load older
                      </Button>
                    </div>
                  )}
                </>
              )}
            </Card>

            <Card className="p-5">
              <SectionHead title="Calls" sub={`${callsQ.data?.total ?? 0} total`} />
              <AsyncState loading={callsQ.isLoading} error={callsQ.error}
                empty={(callsQ.data?.items ?? []).length === 0}
                emptyLabel="No calls recorded with this contact yet.">
                <Table>
                  <THead>
                    <TH>When</TH><TH>Direction</TH><TH>Duration</TH>
                    <TH>Lead</TH><TH>Sentiment</TH><TH>Summary</TH>
                  </THead>
                  <tbody>
                    {(callsQ.data?.items ?? []).map((c) => (
                      <TR key={c.call_id}
                        className={cx(c.call_key && 'cursor-pointer')}
                        onClick={() => c.call_key && nav(`/intelligence/${c.call_key}`)}>
                        <TD className="whitespace-nowrap text-xs">
                          {fmtDateTime(c.started_at)}
                        </TD>
                        <TD><StatusBadge value={c.direction} /></TD>
                        <TD className="tabular-nums">{dur(c.duration_s)}</TD>
                        <TD className="tabular-nums font-semibold">{c.lead_score ?? '—'}</TD>
                        <TD>{c.sentiment ? <StatusBadge value={c.sentiment} /> : '—'}</TD>
                        <TD className="max-w-[280px]">
                          <span className="block truncate text-xs text-dim" title={c.summary ?? ''}>
                            {c.summary ?? '—'}
                          </span>
                        </TD>
                      </TR>
                    ))}
                  </tbody>
                </Table>
              </AsyncState>
            </Card>
          </div>

          {/* ---------------- right 1/3: memory, follow-ups, promises, actions, stats */}
          <div className="space-y-6">
            <Card className="p-5">
              <SectionHead title="Persistent memory"
                right={<UserRound size={15} className="text-dim" />} />
              {!d?.memory_meta ? (
                <p className="text-sm text-dim">Builds automatically after the first analyzed call.</p>
              ) : (
                <div className="space-y-3 text-xs">
                  {factTexts(mem.preferences).length > 0 && (
                    <div>
                      <div className="eyebrow mb-1.5">Interests & preferences</div>
                      <Chips items={factTexts(mem.preferences)} tone="mint" />
                    </div>
                  )}
                  {factTexts(mem.promises, 4).length > 0 && (
                    <div>
                      <div className="eyebrow mb-1.5">Open promises</div>
                      <ul className="list-disc space-y-0.5 pl-4 text-soft">
                        {(mem.promises ?? []).filter((f: any) => (f.status ?? 'active') === 'active')
                          .slice(-4).map((p: any, i: number) => (
                            <li key={i}>{p.text} {p.date && <span className="text-dim">({p.date})</span>}</li>
                          ))}
                      </ul>
                    </div>
                  )}
                  {factTexts(mem.objections, 4).length > 0 && (
                    <div>
                      <div className="eyebrow mb-1.5">Past objections</div>
                      <Chips items={factTexts(mem.objections, 4)} tone="amber" />
                    </div>
                  )}
                  {factTexts(mem.decision_makers, 4).length > 0 && (
                    <div>
                      <div className="eyebrow mb-1.5">Decision makers</div>
                      <Chips items={factTexts(mem.decision_makers, 4)} tone="indigo" />
                    </div>
                  )}
                  {supersededCount > 0 && (
                    <div className="text-2xs text-dim">
                      + {supersededCount} superseded/expired fact(s) kept in history
                    </div>
                  )}
                  <div className="text-2xs text-dim">
                    Memory v{d.memory_meta.version} · {d.memory_meta.calls_count} call(s) merged
                  </div>
                </div>
              )}
            </Card>

            <Card className="p-5" glow={(d?.upcoming_followups?.length ?? 0) > 0 ? 'mint' : undefined}>
              <SectionHead title="Upcoming follow-ups"
                right={<CalendarClock size={15} className="text-dim" />} />
              {(d?.upcoming_followups ?? []).length === 0 ? (
                <p className="text-sm text-dim">Nothing scheduled.</p>
              ) : (
                <ul className="space-y-2.5">
                  {(d?.upcoming_followups ?? []).map((f) => (
                    <li key={f.id} className="rounded-xl bg-[var(--surface-2)] p-3 text-xs">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <StatusBadge value={f.status} />
                        <Badge tone="indigo">{(f.followup_type ?? 'callback').replace(/_/g, ' ')}</Badge>
                        <StatusBadge value={f.priority} />
                      </div>
                      {f.scheduled_at && (
                        <div className="mt-1 font-semibold">
                          {fmtDateTime(f.scheduled_at)}
                        </div>
                      )}
                      <div className="mt-0.5 text-soft">{f.reason ?? f.summary}</div>
                    </li>
                  ))}
                </ul>
              )}
              <Button variant="soft" size="sm" className="mt-3" onClick={() => nav('/followups')}>
                Manage in Follow-ups
              </Button>
            </Card>

            <Card className="p-5">
              <SectionHead title="Open promises"
                right={<Handshake size={15} className="text-dim" />} />
              {(d?.open_commitments ?? []).length === 0 ? (
                <p className="text-sm text-dim">No open promises.</p>
              ) : (
                <ul className="space-y-2.5">
                  {(d?.open_commitments ?? []).map((c) => <CommitmentCard key={c.id} c={c} />)}
                </ul>
              )}
            </Card>

            {(rec.next_actions?.length ?? 0) > 0 && (
              <Card className="p-5">
                <SectionHead title="Recommended next actions"
                  right={<ListTodo size={15} className="text-dim" />} />
                <ul className="list-disc space-y-1 pl-4 text-xs text-soft">
                  {rec.next_actions.map((r: string, i: number) => <li key={i}>{r}</li>)}
                </ul>
              </Card>
            )}

            <Card className="p-5">
              <SectionHead title="Lifetime" />
              <div className="grid grid-cols-2 gap-3">
                <StatTile label="calls" value={d?.stats?.calls ?? 0} />
                <StatTile label="talk time" value={dur(d?.stats?.talk_seconds ?? 0)} />
                <StatTile label="calls · 30 days" value={d?.stats?.calls_30d ?? 0} />
                <StatTile label="first contact" value={fmtDayLabel(d?.stats?.first_call_at)} />
              </div>
              {Object.keys(outcomeMix).length > 0 && (
                <div className="mt-3">
                  <div className="eyebrow mb-1.5">Outcomes</div>
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(outcomeMix).map(([k, n]) => (
                      <Badge key={k} tone={k === 'missed' ? 'rose' : k === 'success' ? 'mint' : 'neutral'} soft>
                        {k.replace(/_/g, ' ')}: {n as number}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          </div>
        </div>
      </AsyncState>

      <Modal open={noteOpen} onClose={() => setNoteOpen(false)} title="Add note"
        footer={
          <>
            <Button variant="ghost" onClick={() => setNoteOpen(false)}>Cancel</Button>
            <Button onClick={saveNote} disabled={!note.trim()}>Save note</Button>
          </>
        }>
        <Field label="Note">
          <Input value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="Anything worth remembering" autoFocus
            onKeyDown={(e) => e.key === 'Enter' && saveNote()} />
        </Field>
      </Modal>
    </div>
  )
}
