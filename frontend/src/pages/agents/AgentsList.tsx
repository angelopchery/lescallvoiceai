import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useGet, useRefresh } from '../../api/hooks/useApi'
import { archiveAgent, archiveConfirmCopy, createTwin, type AgentSummary, type AgentsList as AgentsListT } from '../../api/agents'
import { Card, Button, Badge, EmptyState, IconButton, LiveDot, Spinner, SkeletonBlock, cx } from '../../components/ui'
import { PageHeader, AsyncState } from '../../components/kit'
import {
  ActivateAgentModal, slotFor, type ActivateTarget, type NumberRow,
} from '../../components/ActivateAgent'
import { useToast } from '../../components/Toast'
import { useConfirm } from '../../components/confirm'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Plus, Radio, Pencil, Archive, Bot, Languages, GitBranch, CheckCircle2,
  PhoneIncoming, PhoneOutgoing, CalendarDays, Info,
  MoreVertical, MessageSquare, Share2,
} from 'lucide-react'
import { TryAgentDialog } from '../../components/TryAgent'
import { ImportAgentModal, ShareAgentModal } from '../../components/ShareAgent'
import { useUrlParam } from '../../lib/hooks'



/* ------------------------------------------------------------------ page */

export default function AgentsList() {
  const { data, isLoading, error } = useGet<AgentsListT>(['agents'], '/agents', { refetchInterval: 15000 })
  const { data: numbersData } = useGet<{ numbers: NumberRow[] }>(['numbers'], '/numbers')
  // Plan cap, so the New Agent button can say WHY it is unavailable instead of
  // letting the user fill in a whole form and hit a 402 on submit.
  const { data: billing } = useGet<{
    agents_used: number | null; max_agent_families: number | null
    enforcement: boolean; trial: boolean
  }>(['billing', 'summary'], '/billing/summary')
  // `enforcement || trial`, not `enforcement`. The free plan's 1-agent cap is
  // an anti-abuse limit and binds while the global billing switch stays off,
  // so gating this on `enforcement` alone left the button enabled against a
  // cap the server was already enforcing — the user filled in the whole form
  // and met a 402 on submit.
  const capApplies = !!billing?.enforcement || !!billing?.trial
  const atCap = capApplies
    && billing?.max_agent_families != null
    && (billing.agents_used ?? 0) >= billing.max_agent_families
  const nav = useNavigate()
  const refresh = useRefresh()
  const toast = useToast()
  const confirm = useConfirm()
  const [busy, setBusy] = useState<string>('')
  const [activating, setActivating] = useState<ActivateTarget | null>(null)
  const [trying, setTrying] = useState<{ slug: string; direction: 'inbound' | 'outbound' } | null>(null)
  const [twinBusy, setTwinBusy] = useState<string>('')
  // Sharing is FAMILY-level: one link carries both direction variants,
  // because that is also how the plan cap counts them.
  const [sharing, setSharing] = useState<{ slug: string; title: string } | null>(null)
  // A share link lands here as ?import=<token>. Keeping it in the URL (rather
  // than in component state) is what makes the link survive the login
  // round-trip and a page refresh; clearing it on close stops a dismissed
  // dialog from reappearing on every reload.
  const [importToken, setImportToken] = useUrlParam('import')

  const addTwin = async (source: string, direction: 'inbound' | 'outbound') => {
    setTwinBusy(source)
    try {
      const res = await createTwin(source)
      toast(`Created “${res.name}” — review its opening before you activate it`, 'success')
      refresh(['agents'])
      // Straight into the editor: the adapted copy is a starting point, and
      // the greeting is the first thing worth reading.
      nav(`/build/agents/${res.name}`)
    } catch (e: any) {
      toast(`Could not create the ${direction} version: ${e?.message || e}`, 'error')
    } finally {
      setTwinBusy('')
    }
  }

  const numbers = useMemo(() => numbersData?.numbers ?? [], [numbersData])
  const boundTo = (a: AgentSummary) =>
    numbers.filter((n) => n[slotFor(a.call_direction)] === a.name)

  const onArchive = async (name: string, sibling?: string) => {
    const ok = await confirm({
      title: `Archive "${name}"?`,
      description: archiveConfirmCopy(name, sibling),
      confirmLabel: 'Archive',
      destructive: true,
    })
    if (!ok) return
    setBusy(name)
    try {
      await archiveAgent(name)
      toast(`Archived "${name}"`, 'success')
      refresh(['agents'])
    } catch (e: any) {
      toast(`Archive failed: ${e.message || e}`, 'error')
    } finally {
      setBusy('')
    }
  }

  /* Twins created together (`acme-inbound` / `acme-outbound`) share a family
     and read as ONE logical agent with two direction variants. */
  const families = useMemo(() => {
    const byFamily = new Map<string, AgentSummary[]>()
    for (const a of data?.agents ?? []) {
      const key = a.family || a.name
      byFamily.set(key, [...(byFamily.get(key) ?? []), a])
    }
    return [...byFamily.entries()]
      .map(([key, list]) => {
        // inbound first — the primary variant supplies the card's identity
        const variants = [...list].sort((x, y) =>
          Number(x.call_direction === 'outbound') - Number(y.call_direction === 'outbound'))
        const live = variants.reduce((sum, v) => sum + boundTo(v).length, 0)
        // The direction this family does NOT cover yet, if any. Derived from
        // the server's counterpart fields so the button never offers a slug
        // that already exists (which would 409).
        const source = variants.find((v) => v.counterpart && !v.has_counterpart)
        const missingTwin = variants.length === 1 && source
          ? { source: source.name, slug: source.counterpart!,
              direction: source.counterpart_direction ?? 'outbound' }
          : null
        return { key, variants, primary: variants[0], live, missingTwin }
      })
      .sort((a, b) => (b.live - a.live)
        || (Number(b.variants.some((v) => v.active)) - Number(a.variants.some((v) => v.active))))
  }, [data, numbers])

  return (
    <div className="space-y-6">
      <PageHeader
        title="Agents"
        sub="Build voice agents, chat with them to check the flow, and connect one to your phone number"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {atCap ? (
              // Disabled WITH A REASON. A plain disabled button reads as a
              // bug; naming the cap and the way out (archiving frees a slot)
              // is the difference between "broken" and "understood".
              <div className="flex items-center gap-2">
                <Button variant="primary" disabled
                        title={billing?.trial
                          ? `The free plan includes ${billing?.max_agent_families} agent${billing?.max_agent_families === 1 ? '' : 's'}. Archive it to build a different one, or upgrade for more.`
                          : `Your plan includes ${billing?.max_agent_families} agents. Archive one to free a slot, or upgrade.`}>
                  <Plus size={15} /> New Agent
                </Button>
                <Link to="/billing" className="text-xs font-semibold text-indigo-600 hover:underline dark:text-indigo-300">
                  {billing?.agents_used}/{billing?.max_agent_families} used — upgrade
                </Link>
              </div>
            ) : (
              <Link to="/build/agents/new">
                <Button variant="primary"><Plus size={15} /> New Agent</Button>
              </Link>
            )}
          </div>
        }
      />

      {isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => <SkeletonBlock key={i} h={190} className="rounded-2xl" />)}
        </div>
      ) : (
      <AsyncState error={error}>
        {families.length === 0 ? (
          <EmptyState
            icon={<Bot size={22} />}
            title="No agents yet"
            hint="Agents are voice personas defined in YAML. Build one, chat with it to check the flow, then Activate it on one of your numbers."
            action={
              <Link to="/build/agents/new">
                <Button variant="primary" size="sm"><Plus size={14} /> Create your first agent</Button>
              </Link>
            }
          />
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {families.map((f) => {
              const a = f.primary
              const activated = f.live > 0
              const brokenVariants = f.variants.filter((v) => v.error)
              const missingTwin = f.missingTwin
              return (
              <Card key={f.key} className="flex flex-col gap-3 p-5" hover glow={activated ? 'mint' : undefined}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <h3 className="font-display text-lg font-bold tracking-tight truncate">
                        {a.agent_name || a.name}
                      </h3>
                      {activated && (
                        <Badge tone="mint" soft>
                          <LiveDot />
                          <CheckCircle2 size={12} /> Activated
                        </Badge>
                      )}
                    </div>
                    <div className="truncate text-xs text-dim">{a.company || '—'}</div>
                  </div>
                  <div className="flex max-w-[52%] shrink-0 items-center gap-1">
                    <code title={f.variants.map((v) => v.name).join(', ')} className="min-w-0 truncate rounded-md bg-[var(--surface-2)] px-2 py-0.5 font-mono text-2xs text-soft ring-1 ring-[var(--border)]">
                      {f.key}
                      {a.version && <span className="ml-1 text-2xs text-dim">v{a.version}</span>}
                    </code>
                    {/* One link per agent, covering every direction it has —
                        so this sits on the card, not on a variant row. */}
                    <IconButton
                      label={`Share “${a.agent_name || f.key}” with another account`}
                      disabled={f.variants.every((v) => !!v.error)}
                      className="h-8 w-8"
                      onClick={() => setSharing({ slug: f.key, title: a.agent_name || f.key })}
                    >
                      <Share2 size={14} />
                    </IconButton>
                  </div>
                </div>

                {brokenVariants.length > 0 && (
                  <div role="alert" className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
                    {brokenVariants.map((v) => (
                      <div key={v.name} className="truncate">Invalid YAML in {v.name}: {v.error}</div>
                    ))}
                  </div>
                )}
                {!a.error && (
                  <div className="flex flex-wrap items-center gap-1.5">
                    {(a.languages ?? []).map((l) => (
                      <Badge key={l} tone="neutral">
                        <Languages size={11} /> {l}
                      </Badge>
                    ))}
                    <span title={(a.stages ?? []).join(' → ')} className="inline-flex max-w-full">
                      <Badge tone="neutral">
                        <GitBranch size={11} /> {(a.stages ?? []).length} stages
                      </Badge>
                    </span>
                    {(a.calendar_slots ?? 0) > 0 && (
                      <Badge tone="neutral">
                        <CalendarDays size={11} /> {a.calendar_slots} slots
                      </Badge>
                    )}
                  </div>
                )}

                {/* One row per direction variant — the twins of a single agent. */}
                <div className="mt-1 flex flex-col gap-2.5 border-t border-[var(--border)] pt-3">
                  {f.variants.map((v) => {
                    const outbound = v.call_direction === 'outbound'
                    const bound = boundTo(v)
                    const live = bound.length > 0
                    return (
                      <div key={v.name} className="flex flex-wrap items-center gap-2">
                        <span title={v.name} className="inline-flex max-w-full">
                          <Badge tone={outbound ? 'indigo' : 'mint'}>
                            {outbound ? <PhoneOutgoing size={11} /> : <PhoneIncoming size={11} />}
                            {outbound ? 'outbound' : 'inbound'}
                          </Badge>
                        </span>
                        {live ? (
                          <>
                            <span
                              title={`${outbound ? 'Calling out from' : 'Answering calls on'} ${bound.map((n) => n.number).join(', ')}`}
                              className="inline-flex min-w-0 max-w-full"
                            >
                              <Badge tone="mint">
                                <LiveDot />
                                <Radio size={12} className="shrink-0" />
                                <span className="truncate">Live on {bound.map((n) => n.number).join(', ')}</span>
                              </Badge>
                            </span>
                            <Button variant="ghost" size="sm"
                              onClick={() => setActivating({ slug: v.name, title: v.agent_name || v.name, direction: v.call_direction })}>
                              Change number
                            </Button>
                          </>
                        ) : (
                          <Button variant="soft" size="sm"
                            onClick={() => setActivating({ slug: v.name, title: v.agent_name || v.name, direction: v.call_direction })}
                            disabled={!!v.error}>
                            <Radio size={13} /> Activate
                          </Button>
                        )}
                        <div className="ml-auto flex items-center gap-1">
                          {/* Talk to it before you dial it — a typed call against
                              the live engine. Broken agents can't answer. */}
                          <Button variant="ghost" size="sm" disabled={!!v.error}
                            title={v.error ? 'Fix the agent error before chatting' : `Chat with ${v.name} as if it were a call`}
                            onClick={() => setTrying({ slug: v.name, direction: outbound ? 'outbound' : 'inbound' })}>
                            <MessageSquare size={13} /> Chat with agent
                          </Button>
                          <Link to={`/build/agents/${v.name}`}>
                            <Button variant="ghost" size="sm"><Pencil size={13} /> Edit</Button>
                          </Link>
                          <DropdownMenu>
                            <DropdownMenuTrigger
                              aria-label={`More actions for ${v.name}`}
                              title={`More actions for ${v.name}`}
                              disabled={busy === v.name}
                              className="inline-grid h-9 w-9 shrink-0 place-items-center rounded-md text-soft transition-all hover:bg-[var(--surface-2)] hover:text-[var(--text)] active:scale-[.96] disabled:pointer-events-none disabled:opacity-40 data-[state=open]:bg-[var(--surface-2)] data-[state=open]:text-[var(--text)]"
                            >
                              {busy === v.name ? <Spinner className="h-3.5 w-3.5" /> : <MoreVertical size={15} />}
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem
                                onSelect={() => {
                                  void onArchive(v.name, f.variants.find((o) => o.name !== v.name)?.name)
                                }}
                                className="gap-2 text-rose-600 focus:text-rose-600 dark:text-rose-300 dark:focus:text-rose-300"
                              >
                                <Archive size={13} /> Archive "{v.name}"
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                      </div>
                    )
                  })}

                  {/* A family with one direction can grow the other. Agents
                      created before twins existed — and pairs whose other half
                      was archived — land here. */}
                  {missingTwin && (
                    <div className="flex flex-wrap items-center gap-2.5 rounded-xl border border-dashed border-[var(--border-strong)] px-3.5 py-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5 text-sm font-medium text-soft">
                          {missingTwin.direction === 'outbound'
                            ? <PhoneOutgoing size={13} /> : <PhoneIncoming size={13} />}
                          No {missingTwin.direction} version yet
                        </div>
                        <div className="mt-0.5 text-xs text-dim">
                          {missingTwin.direction === 'outbound'
                            ? 'To call people from this number, you need an outbound agent — it opens the conversation instead of answering.'
                            : 'To answer incoming calls, you need an inbound agent — it picks up instead of dialling out.'}
                        </div>
                      </div>
                      <Button
                        variant="soft" size="sm"
                        className="ml-auto"
                        disabled={twinBusy === missingTwin.source}
                        onClick={() => { void addTwin(missingTwin.source, missingTwin.direction) }}
                        title={`Create ${missingTwin.slug} from ${missingTwin.source}`}
                      >
                        {twinBusy === missingTwin.source
                          ? <Spinner className="h-3 w-3" />
                          : <Plus size={13} />}
                        Add {missingTwin.direction} version
                      </Button>
                    </div>
                  )}
                </div>
              </Card>
              )
            })}
          </div>
        )}
      </AsyncState>
      )}

      <div className="flex items-start gap-2 rounded-xl bg-indigo-500/10 px-4 py-3 ring-1 ring-indigo-500/20">
        <Info size={14} className="mt-0.5 shrink-0 text-indigo-500" />
        <p className="text-xs text-soft">
          <span className="font-semibold">Activating</span> connects an agent to one of your phone
          numbers — the inbound twin answers incoming calls on it, the outbound twin speaks on calls
          placed from it. Manage your numbers in{' '}
          <Link to="/telephony" className="font-semibold text-indigo-600 hover:underline dark:text-indigo-300">Numbers (BYON)</Link>.
        </p>
      </div>

      <ActivateAgentModal
        target={activating}
        open={!!activating}
        onClose={() => setActivating(null)}
      />

      <TryAgentDialog slug={trying?.slug ?? ''} direction={trying?.direction}
        open={!!trying} onOpenChange={(v) => !v && setTrying(null)} />

      <ShareAgentModal
        slug={sharing?.slug ?? ''}
        title={sharing?.title}
        open={!!sharing}
        onClose={() => setSharing(null)}
      />

      <ImportAgentModal
        token={importToken}
        open={!!importToken}
        onClose={() => setImportToken('')}
      />
    </div>
  )
}
