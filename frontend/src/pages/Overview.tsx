import { useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  ArrowRight, BookUser, Bot, CheckCircle2, Circle, Flame, MessageCircle,
  PhoneCall, PhoneForwarded, Radio, Timer,
} from 'lucide-react'
import { PageHeader, AsyncState, ContactLabel, StatusBadge } from '../components/kit'
import { Card, Badge, Button, EmptyState, LiveDot, cx } from '../components/ui'
import { BorderBeam } from '../components/motion'
import { Sparkline } from '../components/charts'
import { CHART } from '../lib/chartColors'
import { StatCard } from '../components/StatCard'
import { useGet } from '../api/hooks/useApi'
import { num } from '../lib/format'

type OverviewT = {
  active_calls: number
  runtime_status: 'healthy' | 'degraded' | 'down'
  kpis: {
    total_calls?: number; inbound_calls?: number; outbound_calls?: number
    answered_calls?: number; missed_calls?: number; minutes_used?: number
  }
  minutes_quota: { total_minutes: number | null; remaining_minutes: number | null }
  charts: { calls_over_time: { date: string; count: number }[] }
}

type HotCall = {
  call_key?: string; customer_name?: string; phone?: string
  /** Saved phone-book name, resolved server-side ('' when not a saved contact). */
  contact_name?: string
  lead_score?: number; summary?: string; started_at?: string
}

/** One row of the "get set up" path. */
function SetupStep({ done, label, hint, to, cta }: {
  done: boolean; label: string; hint: string; to: string; cta: string
}) {
  const nav = useNavigate()
  return (
    <div className={cx('flex items-center gap-3 rounded-xl px-3 py-2.5 transition-colors', !done && 'hover:bg-[var(--surface-2)]')}>
      {done
        ? <CheckCircle2 size={18} className="shrink-0 text-mint-600 dark:text-mint-400" aria-hidden />
        : <Circle size={18} className="shrink-0 text-dim" aria-hidden />}
      <div className="min-w-0 flex-1">
        <div className={cx('text-sm font-medium', done ? 'text-dim line-through decoration-[1.5px]' : 'text-[var(--text)]')}>{label}</div>
        {!done && <div className="text-xs text-dim">{hint}</div>}
      </div>
      {!done && <Button size="sm" variant="ghost" onClick={() => nav(to)}>{cta}</Button>}
    </div>
  )
}

export default function Overview() {
  const nav = useNavigate()
  const ov = useGet<OverviewT>(['overview'], '/overview', { refetchInterval: 30000 })
  const agents = useGet<{ agents?: unknown[] }>(['agents'], '/agents')
  const numbers = useGet<{ numbers?: { inbound_agent_slug?: string | null; outbound_agent_slug?: string | null }[] }>(['numbers'], '/numbers')
  const profile = useGet<{ phone?: string }>(['profile'], '/auth/profile')
  const contacts = useGet<{ total?: number; contacts?: unknown[] }>(['outbound', 'contacts', 'peek'], '/outbound/contacts?page=1&limit=1')
  const hot = useGet<{ items?: HotCall[] }>(['intel', 'hot-leads'], '/intel/calls?page=1&limit=5&min_lead=70')

  const k = ov.data?.kpis
  const series = ov.data?.charts?.calls_over_time ?? []

  const steps = useMemo(() => {
    const agentDone = (agents.data?.agents?.length ?? 0) > 0
    const nums = numbers.data?.numbers ?? []
    const numberDone = nums.length > 0
    const boundDone = nums.some((n) => n.inbound_agent_slug || n.outbound_agent_slug)
    const phoneDone = Boolean(profile.data?.phone)
    const contactsDone = (contacts.data?.total ?? contacts.data?.contacts?.length ?? 0) > 0
    return { agentDone, numberDone, boundDone, phoneDone, contactsDone,
      allDone: agentDone && numberDone && boundDone && phoneDone }
  }, [agents.data, numbers.data, profile.data, contacts.data])

  const hotRows = (hot.data?.items ?? []).filter((r) => r.call_key)

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Overview"
        sub="Your calling operation at a glance."
        actions={
          <>
            <Button variant="ghost" onClick={() => nav('/copilot')}><MessageCircle size={15} /> Ask Copilot</Button>
            {(ov.data?.active_calls ?? 0) > 0 && (
              <Button onClick={() => nav('/live')}><LiveDot /> Watch {ov.data!.active_calls} live</Button>
            )}
          </>
        }
      />

      {/* First-run setup path — disappears once the tenant is fully live. */}
      {!steps.allDone && !agents.isLoading && !numbers.isLoading && (
        <Card glow="mint" className="mb-6 p-4">
          {/* The one beam on this screen: setup is the single thing a
              not-yet-live tenant should look at. */}
          <BorderBeam />
          <div className="mb-1 flex items-center justify-between gap-3 px-3 pt-1">
            <h2 className="font-display text-base font-bold tracking-tight">Get set up</h2>
            <span className="text-xs text-dim">
              {[steps.agentDone, steps.numberDone, steps.boundDone, steps.phoneDone].filter(Boolean).length} of 4 done
            </span>
          </div>
          <div className="grid gap-0.5 sm:grid-cols-2">
            <SetupStep done={steps.agentDone} label="Create your AI agent"
              hint="Describe it to the Copilot — it writes the agent for you." to="/copilot" cta="Build" />
            <SetupStep done={steps.numberDone} label="Connect a phone number"
              hint="Bring your own number from Plivo, Twilio, Telnyx or Vonage." to="/telephony" cta="Connect" />
            <SetupStep done={steps.boundDone} label="Put an agent on a number"
              hint="Bind inbound and outbound so calls reach your agent." to={steps.agentDone ? '/telephony' : '/build/agents'} cta="Activate" />
            <SetupStep done={steps.phoneDone} label="Set your test phone"
              hint="Test calls ring your own phone before you go live." to="/settings" cta="Set" />
          </div>
        </Card>
      )}

      <AsyncState loading={ov.isLoading} error={ov.error}>
        {/* Raw numbers (not num()-formatted strings) so the tiles count up;
            the delays cascade the row left to right on arrival. */}
        <div className="mb-6 grid grid-cols-2 gap-4 xl:grid-cols-4">
          <StatCard label="Calls today" value={k?.total_calls ?? 0} icon={<PhoneCall size={16} />} />
          <StatCard label="Answered" value={k?.answered_calls ?? 0} icon={<CheckCircle2 size={16} />} delay={60} />
          <StatCard label="Minutes used" value={Math.round(k?.minutes_used ?? 0)} icon={<Timer size={16} />} delay={120}
            sub={ov.data?.minutes_quota?.remaining_minutes != null ? `${num(Math.round(ov.data.minutes_quota.remaining_minutes))} left` : undefined} />
          <StatCard label="Live now" value={ov.data?.active_calls ?? 0} icon={<Radio size={16} />} delay={180}
            sub={ov.data?.runtime_status !== 'healthy' ? ov.data?.runtime_status : undefined} />
        </div>

        <div className="grid gap-4 xl:grid-cols-3">
          <Card className="p-5 xl:col-span-2">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="font-display text-base font-bold tracking-tight">Calls · last 14 days</h2>
              <Link to="/activity" className="flex items-center gap-1 text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-300">
                Call activity <ArrowRight size={12} />
              </Link>
            </div>
            {series.length > 0 && series.some((d) => d.count > 0) ? (
              <>
                <Sparkline data={series.map((d) => d.count)} color={CHART.mint} height={72} />
                <div className="mt-1 flex justify-between text-2xs text-dim">
                  <span>{series[0]?.date?.slice(5)}</span>
                  <span>{series[series.length - 1]?.date?.slice(5)}</span>
                </div>
              </>
            ) : (
              <EmptyState icon={<PhoneForwarded size={20} />} title="No calls yet"
                hint={steps.allDone ? 'Start a campaign or share your number — calls will chart here.' : 'Finish the setup steps above to take your first call.'}
                action={steps.allDone ? <Button size="sm" variant="ghost" onClick={() => nav('/outbound')}>Start a campaign</Button> : undefined} />
            )}
          </Card>

          <Card className="p-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="flex items-center gap-1.5 font-display text-base font-bold tracking-tight">
                <Flame size={15} className="text-amber-500" aria-hidden /> Hot leads
              </h2>
              <Link to="/intelligence" className="flex items-center gap-1 text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-300">
                Intelligence <ArrowRight size={12} />
              </Link>
            </div>
            {hotRows.length === 0 ? (
              <EmptyState icon={<Bot size={20} />} title="No hot leads yet"
                hint="Calls scoring 70+ on lead quality appear here automatically." />
            ) : (
              <ul className="space-y-1">
                {hotRows.map((r) => (
                  <li key={r.call_key}>
                    <button type="button" onClick={() => nav(`/intelligence/${r.call_key}`)}
                      className="w-full rounded-xl px-3 py-2 text-left transition-colors hover:bg-[var(--surface-2)]">
                      <div className="flex items-center justify-between gap-2">
                        <ContactLabel name={r.contact_name || r.customer_name} phone={r.phone} />
                        <Badge tone="mint">{r.lead_score}</Badge>
                      </div>
                      {r.summary && <div className="mt-0.5 line-clamp-1 text-xs text-dim">{r.summary}</div>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          {[
            { to: '/contacts', icon: <BookUser size={16} />, label: 'Import contacts', sub: 'CSV upload, groups, phonebook' },
            { to: '/outbound', icon: <PhoneForwarded size={16} />, label: 'Run a campaign', sub: 'Dial a filtered audience' },
            { to: '/followups', icon: <StatusIcon />, label: 'Today’s follow-ups', sub: 'AI-decided callbacks due now' },
          ].map((q) => (
            <button key={q.to} type="button" onClick={() => nav(q.to)}
              className="card card-hover flex items-center gap-3 p-4 text-left">
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[var(--surface-2)] text-indigo-500 dark:text-indigo-300">{q.icon}</span>
              <span className="min-w-0">
                <span className="block text-sm font-semibold">{q.label}</span>
                <span className="block truncate text-xs text-dim">{q.sub}</span>
              </span>
              <ArrowRight size={14} className="ml-auto shrink-0 text-dim" aria-hidden />
            </button>
          ))}
        </div>

        {ov.data?.runtime_status && ov.data.runtime_status !== 'healthy' && (
          <div className="mt-4 flex items-center gap-2 text-xs text-dim">
            Runtime: <StatusBadge value={ov.data.runtime_status} />
          </div>
        )}
      </AsyncState>
    </div>
  )
}

function StatusIcon() {
  return <PhoneCall size={16} />
}
