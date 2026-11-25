/**
 * Conversation Intelligence workspace — one call, everything the pipeline
 * knows about it: executive summary + scores, transcript (search/jump/copy),
 * intelligence sections, commitments, AI recommendations, editable action
 * items, follow-up decision with explanation, customer memory.
 */
import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  BrainCircuit, Check, ChevronDown, ClipboardCopy, Download, Handshake,
  History, ListTodo, RefreshCw, UserRound,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { patch, post } from '../api/mutate'
import type { IntelDetail } from '../api/intel'
import { Badge, Button, Card, EmptyState, IconButton, SectionHead, cx } from '../components/ui'
import { AsyncState, ContactLabel, PageHeader, SearchInput, StatusBadge } from '../components/kit'
import { Chips, CommitmentCard, ScoreRingRow, TimelineList } from '../components/insight'
import { AudioPlayer } from '../components/AudioPlayer'
import { CallPager, useCallSequence } from '../components/CallPager'
import { useToast } from '../components/Toast'
import { fmtDate, fmtDateTime } from '../lib/format'

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="eyebrow mb-1.5">{title}</div>
      {children}
    </div>
  )
}

export default function IntelligenceCall() {
  const { callKey = '' } = useParams()
  const nav = useNavigate()
  const toast = useToast()
  const refresh = useRefresh()
  const q = useGet<IntelDetail>(['intel-call', callKey], `/intel/calls/${callKey}`)
  // The list this call was opened from, if any: Previous/Next walk it, and
  // the breadcrumb returns to it with its filter and page intact.
  const pager = useCallSequence(callKey)

  const [tSearch, setTSearch] = useState('')
  const [matchCursor, setMatchCursor] = useState(-1) // -1 = not yet jumped
  const [moreSignals, setMoreSignals] = useState(false)
  const d = q.data
  const ci = d?.intelligence

  const matchList = useMemo(() => {
    if (!tSearch.trim() || !d) return []
    const needle = tSearch.toLowerCase()
    return d.transcript
      .map((t, i) => (t.text.toLowerCase().includes(needle) ? i : -1))
      .filter((i) => i >= 0)
  }, [tSearch, d])
  const matches = useMemo(() => new Set(matchList), [matchList])
  const currentMatch = matchCursor >= 0 && matchList.length > 0
    ? matchList[matchCursor % matchList.length] : null

  function cycleMatch() {
    if (!matchList.length) return
    const next = (matchCursor + 1) % matchList.length
    setMatchCursor(next)
    document.querySelector(`[data-turn="${matchList[next]}"]`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  function copyTranscript() {
    if (!d) return
    navigator.clipboard.writeText(
      d.transcript.map((t) => `[${t.timestamp}] ${t.speaker}: ${t.text}`).join('\n'))
    toast('Transcript copied', 'success')
  }

  function downloadTranscript() {
    if (!d) return
    const blob = new Blob(
      [d.transcript.map((t) => `[${t.timestamp}] ${t.speaker}: ${t.text}`).join('\n')],
      { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${callKey}-transcript.txt`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  async function toggleActionItem(id: number, status: string) {
    const next = status === 'done' ? 'open' : 'done'
    try {
      await patch(`/intel/action-items/${id}`, { status: next })
      refresh(['intel-call', callKey])
    } catch (e: any) {
      toast(`Update failed: ${e?.message ?? e}`, 'error')
    }
  }

  async function reanalyze() {
    try {
      await post(`/intel/calls/${callKey}/reanalyze`)
      toast('Re-analysis queued — results refresh automatically', 'success')
    } catch (e: any) {
      toast(`Failed: ${e?.message ?? e}`, 'error')
    }
  }

  const rec = ci?.recommendations ?? {}
  const perf = ci?.agent_performance ?? {}

  return (
    <div className="space-y-6">
      {/* Saved contacts are headlined by name; the number then belongs in the
          sub-line, where it stays visible without being the whole identity. */}
      <PageHeader detail
        /* Was a bare '/intelligence', which threw away every filter the user
           had set — including the AI one — on the way back. */
        crumbs={[{ label: 'Intelligence', to: pager.listUrl }]}
        title={d?.call.contact_name || d?.call.phone || callKey}
        sub={d
          ? (d.call.contact_name && d.call.phone ? `${d.call.phone} · ` : '')
            + `${d.call.direction} · ${fmtDateTime(d.call.started_at)}`
            + ` · ${Math.round(d.call.duration_s ?? 0)}s · ${d.call.agent_slug ?? '—'}`
          : undefined}
        actions={
          <>
            {d?.audio_url && <AudioPlayer src={d.audio_url} caller={d.call.phone ?? ''} />}
            <Button variant="ghost" size="sm" onClick={reanalyze} title="Run the pipeline again">
              <RefreshCw size={14} /> Reanalyze
            </Button>
            {/* Last, so it is the outermost control: its Next button keeps the
                same screen position from call to call whether or not a given
                call has a recording to play. */}
            <CallPager pager={pager} />
          </>
        }
      />

      <AsyncState loading={q.isLoading} error={q.error} empty={!d} variant="text" rows={8}>
        {!ci ? (
          <Card className="p-6">
            <EmptyState icon={<BrainCircuit size={22} />} title="Analysis in progress"
              hint="This call has not finished the intelligence pipeline yet. It usually takes under a minute after the call ends." />
          </Card>
        ) : (
          <div className="grid gap-6 xl:grid-cols-3">
            {/* ------------ left 2/3: summary + transcript + intelligence */}
            <div className="space-y-6 xl:col-span-2">
              <Card className="p-5">
                <SectionHead title="Executive summary"
                  right={ci.low_confidence
                    ? <Badge tone="amber">low confidence</Badge>
                    : <Badge tone="mint">confidence {(ci.confidence ?? 0).toFixed(2)}</Badge>} />
                <p className="text-sm leading-relaxed text-soft">{ci.summary}</p>
                {ci.outcome_summary && (
                  <p className="mt-2 text-xs text-dim">Outcome: {ci.outcome_summary}</p>
                )}
                <ScoreRingRow items={[
                  { label: 'Lead', value: ci.lead_score },
                  { label: 'Opportunity', value: ci.opportunity_score },
                  { label: 'Risk', value: ci.risk_score, invert: true },
                  { label: 'Compliance', value: ci.compliance_score },
                ]}>
                  <div className="space-y-1 text-xs">
                    <div>Sentiment: <StatusBadge value={ci.sentiment} /></div>
                    <div>Urgency: <StatusBadge value={ci.urgency} /></div>
                    <div>Conversion: <span className="font-semibold tabular-nums">
                      {Math.round((ci.conversion_probability ?? 0) * 100)}%</span></div>
                  </div>
                </ScoreRingRow>
                {ci.tags?.length > 0 && (
                  <div className="mt-3"><Chips items={ci.tags} tone="indigo" /></div>
                )}
              </Card>

              <Card className="p-5">
                <SectionHead title="Transcript" sub={`${d.transcript.length} turns`}
                  right={
                    <div className="flex items-center gap-2">
                      <SearchInput value={tSearch}
                        onChange={(v) => { setTSearch(v); setMatchCursor(-1) }}
                        onEnter={cycleMatch}
                        placeholder="Find in call…" ariaLabel="Search transcript"
                        className="w-44" />
                      {tSearch.trim() && (
                        <span aria-live="polite"
                          className="whitespace-nowrap text-xs tabular-nums text-dim">
                          {matchList.length === 0 ? 'No matches'
                            : matchCursor >= 0
                              ? `${(matchCursor % matchList.length) + 1} of ${matchList.length}`
                              : `${matchList.length} match${matchList.length === 1 ? '' : 'es'}`}
                        </span>
                      )}
                      <IconButton label="Copy transcript" onClick={copyTranscript}>
                        <ClipboardCopy size={13} />
                      </IconButton>
                      <IconButton label="Download transcript" onClick={downloadTranscript}>
                        <Download size={13} />
                      </IconButton>
                    </div>
                  } />
                <div className="max-h-[420px] space-y-2.5 overflow-y-auto pr-1">
                  {d.transcript.map((t, i) => (
                    <div key={i} data-turn={i}
                      className={cx('flex', t.speaker === 'Agent' ? 'justify-start' : 'justify-end')}>
                      <div className={cx('max-w-[85%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed ring-1',
                        t.speaker === 'Agent'
                          ? 'bg-[var(--surface-2)] ring-[var(--border)]'
                          : 'bg-mint-400/10 ring-mint-400/25',
                        matches.has(i) && (i === currentMatch
                          ? 'ring-2 ring-amber-400'
                          : 'ring-2 ring-amber-400/60'))}>
                        <div className="mb-0.5 text-2xs font-semibold uppercase tracking-wider text-dim">
                          {t.speaker} · {t.timestamp}
                        </div>
                        {t.text}
                      </div>
                    </div>
                  ))}
                  {d.transcript.length === 0 && (
                    <div className="py-8 text-center text-sm text-dim">No transcript captured.</div>
                  )}
                </div>
              </Card>

              <Card className="p-5">
                <SectionHead title="Conversation intelligence" />
                {/* The three signals a sales manager acts on first, promoted. */}
                <div className="grid gap-5 sm:grid-cols-2">
                  <Section title="Buying signals"><Chips items={ci.buying_signals} tone="mint" /></Section>
                  <Section title="Objections"><Chips items={ci.objections} tone="amber" /></Section>
                  <Section title="Compliance issues"><Chips items={ci.compliance_issues} tone="rose" /></Section>
                </div>
                <button type="button"
                  aria-expanded={moreSignals}
                  onClick={() => setMoreSignals((o) => !o)}
                  className="mt-4 inline-flex min-h-[36px] items-center gap-1 rounded-md text-xs font-semibold text-dim transition hover:text-soft sm:min-h-0">
                  <ChevronDown size={14} aria-hidden
                    className={cx('transition-transform', moreSignals && 'rotate-180')} />
                  More signals
                </button>
                {moreSignals && (
                  <div className="mt-4 grid gap-5 sm:grid-cols-2">
                    <Section title="Topics"><Chips items={ci.topics} tone="indigo" /></Section>
                    <Section title="Pain points"><Chips items={ci.pain_points} tone="amber" /></Section>
                    <Section title="Product interest"><Chips items={ci.product_interest} tone="mint" /></Section>
                    <Section title="Competitor mentions"><Chips items={ci.competitor_mentions} tone="rose" /></Section>
                    <Section title="Budget"><Chips items={ci.budget_discussion} /></Section>
                    <Section title="Timeline"><Chips items={ci.timeline_discussion} /></Section>
                    <Section title="Decision makers"><Chips items={ci.decision_makers} /></Section>
                    <Section title="Risk indicators"><Chips items={ci.risk_indicators} tone="rose" /></Section>
                    <Section title="Escalations"><Chips items={ci.escalations} tone="rose" /></Section>
                  </div>
                )}
                {(perf.score != null || perf.strengths?.length) && (
                  <div className="mt-5 rounded-xl bg-[var(--surface-2)] p-4">
                    <div className="eyebrow mb-2">Agent performance {perf.score != null && `· ${perf.score}/100`}</div>
                    <div className="grid gap-3 text-xs sm:grid-cols-2">
                      <div>
                        <div className="mb-1 font-semibold text-mint-600 dark:text-mint-300">Strengths</div>
                        <ul className="list-disc space-y-0.5 pl-4 text-soft">
                          {(perf.strengths ?? []).map((s: string, i: number) => <li key={i}>{s}</li>)}
                        </ul>
                      </div>
                      <div>
                        <div className="mb-1 font-semibold text-amber-600 dark:text-amber-300">Improvements</div>
                        <ul className="list-disc space-y-0.5 pl-4 text-soft">
                          {(perf.improvements ?? []).map((s: string, i: number) => <li key={i}>{s}</li>)}
                        </ul>
                      </div>
                    </div>
                  </div>
                )}
              </Card>

              {ci.key_moments?.length > 0 && (
                <Card className="p-5">
                  <SectionHead title="Timeline" sub="Key moments extracted from the conversation" />
                  <TimelineList events={ci.key_moments.map((m: any, i: number) => ({
                    id: i, title: m.label, detail: m.detail,
                  }))} />
                </Card>
              )}
            </div>

            {/* ------------ right 1/3: follow-up, commitments, actions, memory */}
            <div className="space-y-6">
              <Card className="p-5" glow={d.followup?.decision === 'yes' ? 'mint' : undefined}>
                <SectionHead title="Follow-up decision"
                  right={<History size={15} className="text-dim" />} />
                {d.followup ? (
                  <div className="space-y-2 text-sm">
                    <div className="flex items-center gap-2">
                      <StatusBadge value={d.followup.decision} />
                      <StatusBadge value={d.followup.status} />
                      {d.followup.confidence != null && (
                        <span className="text-2xs text-dim">
                          conf {(d.followup.confidence).toFixed(2)}
                        </span>
                      )}
                    </div>
                    {d.followup.scheduled_at && (
                      <div className="font-semibold">
                        {fmtDateTime(d.followup.scheduled_at)}
                        {d.followup.auto_scheduled ? (
                          <span className="ml-1.5 text-2xs font-normal text-dim">(AI-picked time)</span>
                        ) : null}
                      </div>
                    )}
                    <p className="text-soft">{d.followup.explanation}</p>
                    <Button variant="soft" size="sm" onClick={() => nav('/followups')}>
                      Manage in Follow-ups
                    </Button>
                  </div>
                ) : (
                  <p className="text-sm text-dim">No decision recorded yet.</p>
                )}
              </Card>

              <Card className="p-5">
                <SectionHead title="Commitments"
                  right={<Handshake size={15} className="text-dim" />} />
                {d.commitments.length === 0
                  ? <p className="text-sm text-dim">No promises detected on this call.</p>
                  : (
                    <ul className="space-y-3">
                      {d.commitments.map((c) => <CommitmentCard key={c.id} c={c} />)}
                    </ul>
                  )}
              </Card>

              <Card className="p-5">
                <SectionHead title="Action items"
                  right={<ListTodo size={15} className="text-dim" />} />
                {d.action_items.length === 0
                  ? <p className="text-sm text-dim">No action items.</p>
                  : (
                    <ul className="space-y-2">
                      {d.action_items.map((a) => (
                        <li key={a.id} className="flex items-start gap-2.5 text-sm">
                          <button onClick={() => toggleActionItem(a.id, a.status)}
                            aria-label={a.status === 'done' ? 'Reopen' : 'Complete'}
                            className={cx('mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-md ring-1 transition',
                              a.status === 'done'
                                ? 'bg-mint-500 text-white ring-mint-500'
                                : 'ring-[var(--border)] hover:ring-mint-400')}>
                            {a.status === 'done' && <Check size={13} />}
                          </button>
                          <div className={cx(a.status === 'done' && 'line-through opacity-60')}>
                            <div className="text-soft">{a.description}</div>
                            <div className="text-2xs text-dim">
                              <StatusBadge value={a.priority} />
                              {a.due_at && <span className="ml-1.5">due {fmtDate(a.due_at)}</span>}
                            </div>
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}
              </Card>

              {rec.next_actions?.length > 0 && (
                <Card className="p-5">
                  <SectionHead title="AI recommendations" />
                  <ul className="list-disc space-y-1 pl-4 text-xs text-soft">
                    {rec.next_actions.map((r: string, i: number) => <li key={i}>{r}</li>)}
                    {(rec.documents ?? []).map((r: string, i: number) => (
                      <li key={`d${i}`}>Send document: {r}</li>
                    ))}
                    {(rec.emails ?? []).map((r: string, i: number) => (
                      <li key={`e${i}`}>Email: {r}</li>
                    ))}
                  </ul>
                </Card>
              )}

              <Card className="p-5">
                <SectionHead title="Customer memory"
                  right={<UserRound size={15} className="text-dim" />} />
                {!d.memory ? (
                  <p className="text-sm text-dim">First conversation with this customer.</p>
                ) : (
                  <div className="space-y-3 text-xs">
                    <div className="text-dim">
                      {d.memory.calls_count} call(s) on record
                      {d.memory.risk_level && <> · risk <StatusBadge value={d.memory.risk_level} /></>}
                    </div>
                    {(d.memory.preferences ?? []).length > 0 && (
                      <Section title="Interests">
                        <Chips items={d.memory.preferences.map((p: any) => p.text)} tone="mint" />
                      </Section>
                    )}
                    {(d.memory.objections ?? []).length > 0 && (
                      <Section title="Past objections">
                        <Chips items={d.memory.objections.map((p: any) => p.text)} tone="amber" />
                      </Section>
                    )}
                    {(d.memory.promises ?? []).length > 0 && (
                      <Section title="Open promises">
                        <ul className="list-disc space-y-0.5 pl-4 text-soft">
                          {d.memory.promises.slice(-4).map((p: any, i: number) => (
                            <li key={i}>{p.text} <span className="text-dim">({p.date})</span></li>
                          ))}
                        </ul>
                      </Section>
                    )}
                  </div>
                )}
              </Card>
            </div>
          </div>
        )}
      </AsyncState>
    </div>
  )
}
