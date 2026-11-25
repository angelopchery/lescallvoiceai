import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useGet, useRefresh } from '../api/hooks/useApi'
import {
  copilotChat, copilotSession, copilotSessions, copilotDeleteSession,
  copilotUpload, applyPendingChange, discardPendingChange, copilotQuickControls,
  type CopilotAction, type TranscriptMsg, type SessionSummary,
  type InterviewProgress, type PendingChange, type VoiceSample, type QuickControls,
} from '../api/copilot'
import { DOC_ACCEPT, DOC_MAX_MB, DOC_TYPES_HUMAN, docSupported } from '../api/agents'
import { cx, Badge, Button, EmptyState, IconButton, Spinner } from '../components/ui'
import { TextShimmer } from '../components/motion'
import { Markdown } from '../components/markdown'
import { useVoicePrompt } from '../lib/voicePrompt'
import { parseTs } from '../lib/format'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { TryAgentDialog } from '../components/TryAgent'
import { ActivateAgentModal, type ActivateTarget } from '../components/ActivateAgent'
import { VoiceSpeedControls } from '../components/VoiceSpeedControls'
import {
  Sparkles, SendHorizontal, RotateCcw, Check, X, ArrowDown, Info,
  Plus, Pencil, Radio, Bot, Trash2, MessagesSquare, SquarePen, Lightbulb,
  Paperclip, FileText, ClipboardList, Eye, EyeOff, PlayCircle, Volume2,
  Mic, Square, Languages, SlidersHorizontal,
} from 'lucide-react'

/* ─────────────────────────── session id ─────────────────────────── */

const SID_KEY = 'copilot_session_id'

function newSessionId() {
  return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
}

/**
 * Opening Copilot always starts a BLANK chat, the way ChatGPT does.
 *
 * This used to read the last session id out of localStorage and resume it, so
 * navigating to Copilot dropped you into whatever conversation you happened to
 * leave open — days later, mid-interview, with no indication of why. Earlier
 * chats are not lost: they are in the Chats sidebar (and deep links still work,
 * see the ?session= param below), which is where resuming belongs — a
 * deliberate choice, not a side effect of a stale key.
 *
 * Continuity across a refresh comes from the URL instead: the active session is
 * mirrored into ?session= (see syncSessionUrl), so reloading keeps your place
 * while a clean visit to /copilot is always a clean chat. That also makes a
 * conversation linkable — which is what "open this agent's chat" needs.
 */
function loadSessionId() {
  const deepLink = new URLSearchParams(window.location.search).get('session')
  if (deepLink && /^[A-Za-z0-9_-]{1,80}$/.test(deepLink)) return deepLink
  const sid = newSessionId()
  localStorage.setItem(SID_KEY, sid)
  return sid
}

/** Mirror the active session into the URL without adding a history entry. */
function syncSessionUrl(sid: string) {
  const url = new URL(window.location.href)
  if (url.searchParams.get('session') === sid) return
  url.searchParams.set('session', sid)
  window.history.replaceState(null, '', url)
}

function relTime(iso?: string | null): string {
  const at = parseTs(iso)
  if (!at) return ''
  const mins = Math.max(0, Math.round((Date.now() - at.getTime()) / 60000))
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.round(hours / 24)
  return days < 7 ? `${days}d` : at.toLocaleDateString('en-IN')
}

/* Local message shape: TranscriptMsg plus a frontend-only error flag so
   failed turns render distinctly and offer a Retry affordance. */
type ChatMsg = TranscriptMsg & { isError?: boolean }

/* ─────────────────────────── history sidebar ─────────────────────────── */

type SessionListProps = {
  sessions: SessionSummary[]
  activeSid: string
  onSelect: (sid: string) => void
  onNew: () => void
  onDelete: (sid: string) => void
}

function SessionList({ sessions, activeSid, onSelect, onNew, onDelete }: SessionListProps) {
  // Inline two-step delete confirm (replaces the native confirm() dialog).
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  useEffect(() => {
    if (!confirmingId) return
    const t = setTimeout(() => setConfirmingId(null), 3000)
    return () => clearTimeout(t)
  }, [confirmingId])

  return (
    <>
      <Button variant="ghost" onClick={onNew} className="justify-start" title="Start a new conversation">
        <SquarePen size={15} className="text-mint-600 dark:text-mint-300" /> New chat
      </Button>
      <div className="eyebrow mt-2 px-1">Previous chats</div>
      <div className="flex-1 space-y-1 overflow-y-auto pr-0.5">
        {sessions.length === 0 && (
          <div className="flex items-center gap-2 px-1 py-2 text-xs text-dim">
            <MessagesSquare size={13} /> No conversations yet
          </div>
        )}
        {sessions.map((s) => (
          <div
            key={s.session_id}
            className={cx(
              'group relative rounded-lg ring-1 transition',
              s.session_id === activeSid
                ? 'bg-mint-400/10 ring-mint-400/30'
                : 'bg-transparent ring-transparent hover:bg-[var(--surface-2)] hover:ring-[var(--border)]',
            )}
          >
            <button
              onClick={() => onSelect(s.session_id)}
              aria-current={s.session_id === activeSid ? 'true' : undefined}
              className="w-full px-2.5 py-2 text-left"
            >
              <div className="flex items-center gap-1.5">
                <span className={cx(
                  'truncate text-sm font-semibold',
                  s.session_id === activeSid ? 'text-mint-700 dark:text-mint-300' : 'text-[var(--text)]',
                )}>
                  {s.title || 'New chat'}
                </span>
                <span className="ml-auto shrink-0 text-2xs text-dim">{relTime(s.updated_at)}</span>
                {s.message_count != null && (
                  <span className="shrink-0 pr-7 text-2xs tabular-nums text-dim">· {s.message_count}</span>
                )}
              </div>
              {s.agent && (
                <div className="mt-0.5 flex items-center gap-1 text-2xs text-dim">
                  <Bot size={11} /> <span className="truncate">{s.agent_name || s.agent}</span>
                </div>
              )}
            </button>
            {confirmingId === s.session_id ? (
              <div className="absolute inset-y-1 right-1 z-10 flex items-center gap-1.5 rounded-md bg-[var(--surface-solid)] px-1 shadow-glass">
                <Button
                  size="sm"
                  variant="danger"
                  title="Confirm delete"
                  onClick={() => { setConfirmingId(null); onDelete(s.session_id) }}
                >
                  <Check size={13} /> Delete
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  title="Cancel delete"
                  onClick={() => setConfirmingId(null)}
                >
                  <X size={13} />
                </Button>
              </div>
            ) : (
              <button
                onClick={(e) => { e.stopPropagation(); setConfirmingId(s.session_id) }}
                title="Delete chat"
                aria-label="Delete chat"
                className="absolute right-1 top-1 rounded-md p-2 text-dim opacity-60 transition hover:bg-rose-500/10 hover:text-rose-500 focus-visible:opacity-100 md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100"
              >
                <Trash2 size={14} />
              </button>
            )}
          </div>
        ))}
      </div>
    </>
  )
}

function HistorySidebar(props: SessionListProps) {
  return (
    <aside className="hidden w-60 shrink-0 flex-col gap-2 md:flex">
      <SessionList {...props} />
    </aside>
  )
}

/* ─────────────────────────── pieces ─────────────────────────── */

function CopilotAvatar() {
  return (
    <div className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-mint-300 to-indigo-500 text-[#04150d] shadow-glow-mint">
      <Sparkles size={15} />
    </div>
  )
}

/* The backend tags every action with the agent slug it touched (may be "").
   The shared CopilotAction type doesn't model it, so widen locally. */
type ActionWithAgent = CopilotAction & { agent?: string }

function ActionChips({ actions, summary, agentLinks }: {
  actions?: CopilotAction[]; summary?: string; agentLinks?: string[]
}) {
  const [trying, setTrying] = useState<string | null>(null)
  const [activating, setActivating] = useState<ActivateTarget | null>(null)
  // The agent list is already loaded for the sidebar; it carries call_direction,
  // which decides whether activating binds the inbound or the outbound slot.
  const { data: agentsData } = useGet<{ agents?: { name: string; agent_name?: string; call_direction?: string }[] }>(
    ['agents'], '/agents')
  if (!actions?.length) return null
  // The server sends agent_links = agents that EXIST on disk (a draft still
  // being interviewed is not tryable — dead "Try …" buttons, 2026-08-01).
  // Deriving from raw actions is only a fallback for responses that predate
  // the field.
  const agents = agentLinks ?? [...new Set(
    (actions as ActionWithAgent[]).map((a) => (a.agent || '').trim()).filter(Boolean),
  )]
  // One plain-English line replaces the old per-tool chip list — the raw
  // tool names and error internals confused non-technical users (support
  // complaint, 2026-08-01). The server writes the line; tool detail stays
  // in `actions` for the quick-action buttons below.
  const allOk = actions.every((a) => a.ok)
  return (
    <div className="mt-2">
      {summary && (
        <p className="flex items-start gap-1.5 text-xs text-dim">
          {allOk
            ? <Check size={13} className="mt-px shrink-0 text-mint-500" />
            : <Info size={13} className="mt-px shrink-0 text-amber-500" />}
          <span>{summary}</span>
        </p>
      )}
      {/* Per-agent quick actions — the copilot just changed this agent, so the
          next thing you want is to hear it, not to go hunting for it. "Try it"
          runs a real engine turn in text before anyone dials the number. */}
      {agents.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {agents.map((slug) => {
            // Buttons say the agent's spoken name, not the internal slug —
            // "Try Vishal", never "Try pramukh_agent".
            const meta = (agentsData?.agents ?? []).find((x) => x.name === slug)
            const disp = meta?.agent_name || slug
            return (
            <span key={slug} className="inline-flex flex-wrap gap-1.5">
              <button type="button" onClick={() => setTrying(slug)}
                className="inline-flex max-w-full rounded-full"
                title={`Try ${disp} — one live engine turn, in text`}>
                <Badge tone="mint">
                  <PlayCircle size={11} /> <span className="truncate">Try {disp}</span>
                </Badge>
              </button>
              <Link
                to={`/build/agents/${slug}`}
                className="inline-flex max-w-full rounded-full"
                title={`Open ${disp} in the agent editor`}
              >
                <Badge tone="indigo">
                  <Pencil size={11} /> <span className="truncate">Open {disp} in editor</span>
                </Badge>
              </Link>
              {/* Same modal the Agents panel uses — an agent is only real once
                  it is on a number, so that step belongs here, not one
                  navigation away. */}
              <button type="button"
                onClick={() => {
                  setActivating({ slug, title: disp,
                    direction: meta?.call_direction })
                }}
                className="inline-flex max-w-full rounded-full"
                title={`Put ${disp} on one of your numbers`}>
                <Badge tone="amber">
                  <Radio size={11} /> <span className="truncate">Activate {disp}</span>
                </Badge>
              </button>
            </span>
            )
          })}
        </div>
      )}
      <TryAgentDialog slug={trying ?? ''} open={!!trying}
        onOpenChange={(v) => !v && setTrying(null)} />
      <ActivateAgentModal target={activating} open={!!activating}
        onClose={() => setActivating(null)} />
    </div>
  )
}

/* Assistant prose is Markdown — `**bold**`, bullets, headings and ``` fences
   all render as themselves instead of as punctuation. See components/markdown. */
function AssistantText({ content }: { content: string }) {
  return <Markdown text={content} />
}

/* Playable voice previews — rendered when the copilot names TTS voices while
   discussing them, so the user hears a voice before committing to it. One
   shared <audio> element: starting a sample stops the previous one. */
function VoicePreviewRow({ samples }: { samples?: VoiceSample[] }) {
  const [playing, setPlaying] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  useEffect(() => () => { audioRef.current?.pause() }, [])
  if (!samples?.length) return null
  const toggle = (s: VoiceSample) => {
    const a = audioRef.current
    if (playing === s.speaker && a) {
      a.pause()
      setPlaying(null)
      return
    }
    a?.pause()
    const next = new Audio(s.url)
    audioRef.current = next
    next.onended = () => setPlaying(null)
    next.onerror = () => setPlaying(null)
    next.play().then(() => setPlaying(s.speaker)).catch(() => setPlaying(null))
  }
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {samples.map((s) => (
        <button
          key={s.speaker}
          type="button"
          onClick={() => toggle(s)}
          className="inline-flex rounded-full"
          title={`Play a sample of ${s.speaker} (${s.gender} voice)`}
        >
          <Badge tone={playing === s.speaker ? 'mint' : 'indigo'}>
            {playing === s.speaker ? <Volume2 size={11} /> : <PlayCircle size={11} />}
            <span className="capitalize">{s.speaker}</span>
            <span className="opacity-60">{s.gender === 'female' ? '♀' : '♂'}</span>
          </Badge>
        </button>
      ))}
    </div>
  )
}

function Bubble({ msg, onRetry }: { msg: ChatMsg; onRetry?: () => void }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[82%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-gradient-to-b from-mint-300 to-mint-500 px-4 py-2.5 text-sm font-medium leading-relaxed text-[#04150d] shadow-glow-mint">
          {msg.content}
        </div>
      </div>
    )
  }
  return (
    <div className="flex items-start gap-2.5">
      <CopilotAvatar />
      <div className="max-w-[82%]">
        <div className={cx(
          'whitespace-pre-wrap break-words rounded-2xl rounded-tl-md px-4 py-2.5 text-sm leading-relaxed ring-1',
          msg.isError
            ? 'bg-rose-500/10 text-rose-600 ring-rose-500/25 dark:text-rose-300'
            : 'bg-[var(--surface-2)] text-[var(--text)] ring-[var(--border)]',
        )}>
          <AssistantText content={msg.content} />
        </div>
        {msg.isError && onRetry && (
          <div className="mt-2">
            <Button variant="ghost" size="sm" onClick={onRetry} title="Re-send the last message">
              <RotateCcw size={13} /> Retry
            </Button>
          </div>
        )}
        <VoicePreviewRow samples={msg.voice_samples} />
        <ActionChips actions={msg.actions} summary={msg.actions_summary}
          agentLinks={msg.agent_links} />
      </div>
    </div>
  )
}

function TypingBubble() {
  // Phase caption keeps long tool-running turns (10-30s) from reading as a hang.
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => clearInterval(t)
  }, [])
  const caption = elapsed < 4
    ? 'Thinking…'
    : elapsed < 12
      ? 'Working on it — running actions…'
      : 'Still working. Building or editing an agent can take up to a minute.'
  return (
    <div className="flex items-start gap-2.5">
      <CopilotAvatar />
      <div className="flex flex-col rounded-2xl rounded-tl-md bg-[var(--surface-2)] px-4 py-3 ring-1 ring-[var(--border)]">
        <div className="flex items-center gap-1.5 py-1">
          {[0, 150, 300].map((delay) => (
            <span
              key={delay}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-[var(--text)] opacity-40"
              style={{ animationDelay: `${delay}ms` }}
            />
          ))}
        </div>
        <div className="mt-1 text-2xs"><TextShimmer>{caption}</TextShimmer></div>
      </div>
    </div>
  )
}

/* ─────────────── interview progress rail + spec preview (v2) ─────────────── */

function InterviewRail({ progress }: { progress: InterviewProgress }) {
  const done = progress.layers.filter((l) => l.done || l.skipped).length
  return (
    <div className="mb-2 rounded-xl bg-[var(--surface-2)] px-3 py-2 ring-1 ring-[var(--border)]">
      <div className="flex items-center gap-2">
        <ClipboardList size={13} className="shrink-0 text-mint-600 dark:text-mint-300" />
        <span className="text-xs font-semibold text-soft">
          Building your agent — {done}/{progress.layers.length} topics covered
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {progress.layers.map((l) => (
          <span
            key={l.id}
            title={l.skipped ? 'Skipped at your request' : l.done ? 'Covered' : 'Still to cover'}
            className={cx('inline-flex max-w-full', l.skipped && 'line-through opacity-70')}
          >
            <Badge
              tone={l.done ? 'mint' : l.id === progress.next_layer_id ? 'amber' : 'neutral'}
              soft={!l.done && l.id !== progress.next_layer_id}
            >
              {l.done ? <Check size={10} /> : null}
              {l.title}
            </Badge>
          </span>
        ))}
      </div>
    </div>
  )
}

function previewSections(draft: Record<string, any>): { title: string; rows: string[] }[] {
  const out: { title: string; rows: string[] }[] = []
  const id = draft.identity || {}
  const who: string[] = []
  if (id.agent_name) who.push(`Name: ${id.agent_name}`)
  if (id.company_name) who.push(`Company: ${id.company_name}`)
  if (id.role_description) who.push(`Role: ${id.role_description}`)
  if (draft.call_direction) who.push(`Direction: ${draft.call_direction}`)
  if (who.length) out.push({ title: 'Who it is', rows: who })
  const langs = draft.languages || {}
  const speak: string[] = []
  if (langs.supported?.length) speak.push(`Languages: ${langs.supported.join(', ')}`)
  if (langs.default) speak.push(`Opens in: ${langs.default}`)
  const voices = langs.tts_voices ? Object.values(langs.tts_voices) as any[] : []
  if (voices[0]?.speaker) speak.push(`Voice: ${voices[0].speaker}`)
  const pers = draft.personality || {}
  if (pers.tone || pers.energy) speak.push(`Style: ${[pers.tone, pers.energy].filter(Boolean).join(', ')}`)
  if (speak.length) out.push({ title: 'How it speaks', rows: speak })
  if (draft.call_flow?.call_goal) {
    out.push({ title: 'Mission', rows: [draft.call_flow.call_goal] })
  }
  const stages = draft.call_flow?.stages
  if (stages?.length) {
    out.push({ title: 'Call flow', rows: stages.map((s: any) => s.id).filter(Boolean) })
  }
  if (draft.slots?.length) {
    out.push({
      title: 'What it collects',
      rows: draft.slots.map((s: any) => s.name).filter(Boolean),
    })
  }
  const kbText = draft.knowledge_base?.portfolio_prompt_text
  const catalog = draft.knowledge_base?.catalog
  if (kbText || catalog?.length) {
    const rows: string[] = []
    if (catalog?.length) rows.push(`${catalog.length} catalog item(s): ${catalog.map((c: any) => c.name).filter(Boolean).join(', ')}`)
    if (kbText) rows.push(String(kbText).slice(0, 160) + (String(kbText).length > 160 ? '…' : ''))
    out.push({ title: 'What it knows', rows })
  }
  const gr = draft.guardrails || {}
  const rules = [...(gr.never_rules || []).map((r: string) => `Never: ${r}`),
                 ...(gr.always_rules || []).map((r: string) => `Always: ${r}`)]
  if (rules.length) out.push({ title: 'Rules', rows: rules.slice(0, 6) })
  const greeting = draft.response_bank?.opening_greeting
  if (greeting) out.push({ title: 'Opening line', rows: [String(greeting)] })
  return out
}

function SpecPreview({ progress }: { progress: InterviewProgress }) {
  const sections = previewSections(progress.draft || {})
  if (!sections.length) return null
  return (
    <div className="space-y-3">
      <div className="eyebrow px-1">Agent so far</div>
      {sections.map((s) => (
        <div key={s.title} className="rounded-xl bg-[var(--surface-2)] p-2.5 ring-1 ring-[var(--border)]">
          <div className="text-2xs font-semibold text-mint-700 dark:text-mint-300">{s.title}</div>
          <ul className="mt-1 space-y-0.5">
            {s.rows.map((r, i) => (
              <li key={i} className="break-words text-xs leading-snug text-soft">{r}</li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

/* ───────────────────── pending-change diff cards (v2) ───────────────────── */

function DiffCards({ changes, busy, onApply, onDiscard }: {
  changes: PendingChange[]
  busy: boolean
  onApply: (id: string) => void
  onDiscard: (id: string) => void
}) {
  if (!changes.length) return null
  return (
    <div className="space-y-2">
      {changes.map((c) => (
        <div key={c.change_id} className="rounded-xl bg-amber-400/8 p-3 ring-1 ring-amber-400/25">
          <div className="flex items-center gap-2 text-xs font-semibold text-soft">
            <Pencil size={13} className="text-amber-500" />
            Proposed change{c.slug ? ` to ${c.slug}` : ''}
            {c.reason ? <span className="font-normal text-dim">— {c.reason}</span> : null}
          </div>
          <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-[var(--bg-2)] px-3 py-2 font-mono text-2xs leading-relaxed ring-1 ring-[var(--border)]">
            {c.diff}
          </pre>
          <div className="mt-2 flex gap-2">
            <Button size="sm" disabled={busy} onClick={() => onApply(c.change_id)} title="Apply this change">
              <Check size={13} /> Apply
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => onDiscard(c.change_id)} title="Discard this change">
              <X size={13} /> Discard
            </Button>
          </div>
        </div>
      ))}
    </div>
  )
}

const SUGGESTIONS = [
  {
    icon: Plus,
    title: 'Create a new agent',
    sub: 'I will interview you and build it step by step',
    prompt: 'I want to create a new voice agent.',
  },
  {
    icon: Pencil,
    title: 'Edit an existing agent',
    sub: 'Change the greeting, voice, flow, or knowledge',
    prompt: 'I want to edit one of my existing agents.',
  },
  {
    icon: Radio,
    title: 'Which agent is live right now?',
    sub: 'See what answers your inbound calls',
    prompt: 'Which agent is live right now?',
  },
]

/* Placeholder tip — the four live templates the call engine substitutes into
   authored spoken lines. Shown in the empty state and (dismissibly) above the
   composer so agent builders discover them mid-conversation too.

   Deliberately short: this is a discovery hint, not documentation. It names
   what exists, shows one line using all four, and gives the single caveat that
   would otherwise surprise someone — the gendered two need a Gender column and
   most contact lists have none. The long form lives in /developers. */
const TIP_KEY = 'copilot_placeholder_tip_dismissed'

function PlaceholderTip({ onDismiss }: { onDismiss?: () => void }) {
  return (
    <div className="flex items-start gap-2.5 rounded-xl bg-amber-400/10 px-3 py-2.5 text-left ring-1 ring-amber-400/25">
      <Lightbulb size={15} className="mt-0.5 shrink-0 text-amber-500" />
      <div className="min-w-0 flex-1 text-xs leading-relaxed text-soft">
        <span className="font-semibold text-[var(--text)]">Tip — greeting placeholders:</span>{' '}
        <code className="rounded bg-[var(--bg-2)] px-1 py-0.5 font-mono text-2xs">{'{contact_name}'}</code> is the dialed contact's name,{' '}
        <code className="rounded bg-[var(--bg-2)] px-1 py-0.5 font-mono text-2xs">{'{current_time}'}</code> becomes morning, afternoon or evening,{' '}
        <code className="rounded bg-[var(--bg-2)] px-1 py-0.5 font-mono text-2xs">{'{honorific}'}</code> becomes Sir or Ma'am, and{' '}
        <code className="rounded bg-[var(--bg-2)] px-1 py-0.5 font-mono text-2xs">{'{title}'}</code> becomes Mr. or Mrs. — e.g.{' '}
        <span className="italic">"Good {'{current_time}'} {'{honorific}'}, am I speaking with {'{title}'} {'{contact_name}'}?"</span>{' '}
        <span className="text-dim">
          The last two need a <span className="font-mono text-2xs">Gender</span> column in your
          contact sheet — a contact without one is addressed by name only, never guessed. The
          agent's own he/she follows the voice you pick.
        </span>
      </div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          title="Dismiss tip"
          aria-label="Dismiss tip"
          className="shrink-0 -m-1 rounded-md p-2 text-dim transition hover:bg-[var(--surface-2)] hover:text-soft"
        >
          <X size={14} />
        </button>
      )}
    </div>
  )
}

/* First-run hero — built on the shared EmptyState, with the suggestion cards
   and placeholder tip supplied as its action slot. */
function ChatEmpty({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center text-center">
      <EmptyState
        icon={<Sparkles size={22} />}
        title="Agent Copilot"
        hint="Describe the voice agent you need — I'll ask a few questions, then build it, wire it up, and put it on your phone line when you're ready."
        action={
          <div className="w-[min(42rem,88vw)] space-y-4">
            <div className="grid gap-3 sm:grid-cols-3">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s.title}
                  onClick={() => onPick(s.prompt)}
                  className="card card-hover flex flex-col items-start gap-2 p-4 text-left transition-all"
                >
                  <div className="grid h-8 w-8 place-items-center rounded-xl bg-[var(--surface-2)] text-mint-600 ring-1 ring-[var(--border)] dark:text-mint-300">
                    <s.icon size={15} />
                  </div>
                  <div className="text-sm font-semibold leading-tight">{s.title}</div>
                  <div className="text-xs leading-snug text-dim">{s.sub}</div>
                </button>
              ))}
            </div>
            <PlaceholderTip />
          </div>
        }
      />
    </div>
  )
}

/* ─────────────────────────── page ─────────────────────────── */

export default function CopilotChat() {
  const [sid, setSid] = useState<string>(() => loadSessionId())
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadingTranscript, setLoadingTranscript] = useState(false)
  const [mobileChatsOpen, setMobileChatsOpen] = useState(false)
  const [showJump, setShowJump] = useState(false)
  const [tipDismissed, setTipDismissed] = useState(() => localStorage.getItem(TIP_KEY) === '1')
  // v2: interview progress rail + live spec preview + pending diff cards
  const [interview, setInterview] = useState<InterviewProgress | null>(null)
  const [pendingChanges, setPendingChanges] = useState<PendingChange[]>([])
  const [showPreview, setShowPreview] = useState(true)
  // Quick controls (voice + speed by click) for whatever this chat edits.
  const [quick, setQuick] = useState<QuickControls | null>(null)
  const [quickOpen, setQuickOpen] = useState(false)
  const [attachments, setAttachments] = useState<{ doc_id: string; name: string }[]>([])
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const nearBottomRef = useRef(true)
  const lastUserRef = useRef('')
  const refresh = useRefresh()

  // Speak instead of type. Dictated text lands in the composer for review and
  // is never sent on its own: a mis-heard word here becomes an instruction to
  // edit a real agent, so the user gets the last look.
  const dictatedRef = useRef('')
  const insertDictation = useCallback((text: string) => {
    setInput((prev) => (prev.trim() ? `${prev.trim()} ${text}` : text))
    dictatedRef.current = text
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      autoGrow(el)
      el.selectionStart = el.selectionEnd = el.value.length
    })
  }, [])
  const voice = useVoicePrompt({ onText: insertDictation })

  // Hindi and Gujarati arrive romanized ("Aap kaise ho") because that is how
  // this box is written in. But someone dictating the literal text of an
  // agent's Hindi greeting wants देवनागरी — and only they know which of the
  // two they are doing, so the original is one click away. No-ops if they
  // have already edited what was inserted.
  const useOriginalScript = () => {
    const raw = voice.last?.raw_text
    const inserted = dictatedRef.current
    if (!raw || !inserted) return
    setInput((prev) => (prev.endsWith(inserted)
      ? prev.slice(0, prev.length - inserted.length) + raw
      : prev))
    dictatedRef.current = raw
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (el) { el.focus(); autoGrow(el) }
    })
  }

  const loadHistory = useCallback(() => {
    copilotSessions()
      .then((res) => setSessions(res.sessions ?? []))
      .catch(() => { /* history is best-effort */ })
  }, [])

  // Restore transcript on mount / session switch. Only replace when the
  // server actually has history — switching to a brand-new/empty session must
  // not wipe messages we just appended locally (see the fresh-session retry
  // in send()). Explicit "New chat" clears via newChat().
  const loadQuick = useCallback(() => {
    copilotQuickControls(sid)
      .then(setQuick)
      .catch(() => setQuick(null))
  }, [sid])

  useEffect(() => {
    let cancelled = false
    syncSessionUrl(sid)
    loadQuick()
    copilotSession(sid)
      .then((res) => {
        if (!cancelled && (res.transcript?.length ?? 0) > 0) {
          setMessages(res.transcript)
          // The rail, preview and diff cards survive a reload too.
          setInterview(res.interview ?? null)
          setPendingChanges(res.pending_changes ?? [])
        }
      })
      .catch(() => { /* fresh session, backend restart, or not ours — leave as-is */ })
      .finally(() => { if (!cancelled) setLoadingTranscript(false) })
    return () => { cancelled = true }
  }, [sid])

  // Chat history for the sidebar
  useEffect(() => { loadHistory() }, [loadHistory])

  // Auto-scroll to the newest message — but only when the user is already
  // near the bottom; otherwise offer a "Latest" pill instead of yanking them.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (nearBottomRef.current) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    } else if (messages.length > 0) {
      setShowJump(true)
    }
  }, [messages, busy])

  const onMessagesScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 120
    nearBottomRef.current = near
    if (near) setShowJump(false)
  }

  const jumpToLatest = () => {
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    nearBottomRef.current = true
    setShowJump(false)
  }

  const send = async (text?: string, opts?: { skipEcho?: boolean }) => {
    const content = (text ?? input).trim()
    if (!content || busy) return
    lastUserRef.current = content
    if (!opts?.skipEcho) {
      setInput('')
      if (inputRef.current) inputRef.current.style.height = 'auto'
      setMessages((m) => [...m, { role: 'user', content }])
    }
    setBusy(true)
    const docIds = attachments.map((a) => a.doc_id)
    setAttachments([])
    try {
      let res
      try {
        res = await copilotChat(sid, content, docIds)
      } catch (e: any) {
        // A stored session id can point at a chat we don't own (a legacy chat,
        // or one from another account on a shared browser); the backend 404s
        // to protect it. Silently start a fresh session and retry once.
        const msg = String(e?.message || e).toLowerCase()
        if (msg.includes('session not found') || msg.includes('404')) {
          const fresh = newSessionId()
          localStorage.setItem(SID_KEY, fresh)
          setSid(fresh)
          res = await copilotChat(fresh, content, docIds)
        } else {
          throw e
        }
      }
      // Adopt the server's real session id (it may have remapped a stale one)
      // so localStorage and future turns stay aligned.
      const realSid = res.session?.session_id
      if (realSid && realSid !== sid) {
        localStorage.setItem(SID_KEY, realSid)
        setSid(realSid)
      }
      setMessages((m) => [...m, { role: 'assistant', content: res.reply, actions: res.actions, actions_summary: res.actions_summary, agent_links: res.agent_links, voice_samples: res.voice_samples }])
      setInterview(res.interview ?? null)
      setPendingChanges(res.pending_changes ?? [])
      if (res.agents_changed) refresh(['agents'])
      loadHistory() // title/agent may have changed — keep the sidebar in sync
      loadQuick()   // the chat may have created the agent or changed its voice
    } catch (e: any) {
      setMessages((m) => [
        ...m,
        {
          role: 'assistant',
          content: `Sorry — I hit an error talking to the copilot backend (${e?.message || e}). Please try again.`,
          actions: [],
          isError: true,
        },
      ])
    } finally {
      setBusy(false)
      inputRef.current?.focus()
    }
  }

  // Re-send the last user message after a failed turn without echoing it again.
  const retryLast = () => {
    if (!lastUserRef.current || busy) return
    void send(lastUserRef.current, { skipEcho: true })
  }

  const newChat = () => {
    const fresh = newSessionId()
    localStorage.setItem(SID_KEY, fresh)
    setSid(fresh)
    setMessages([])
    setInput('')
    setInterview(null)
    setPendingChanges([])
    setQuick(null)
    setAttachments([])
    setLoadingTranscript(false)
    inputRef.current?.focus()
  }

  const attachFile = async (file: File) => {
    // Same gate as the Builder: the server would refuse these anyway, but
    // with the sentence in the chat instead of after a full upload.
    const refusal = !docSupported(file.name)
      ? `Couldn't attach "${file.name}" — upload ${DOC_TYPES_HUMAN}.`
      : file.size > DOC_MAX_MB * 1048576
        ? `Couldn't attach "${file.name}" — it is ${(file.size / 1048576).toFixed(1)} MB and the limit is ${DOC_MAX_MB} MB.`
        : ''
    if (refusal) {
      setMessages((m) => [...m, { role: 'assistant', isError: true, actions: [], content: refusal }])
      if (fileRef.current) fileRef.current.value = ''
      return
    }
    setUploading(true)
    try {
      const res = await copilotUpload(sid, file)
      // The upload may have remapped a stale session id — adopt it.
      if (res.session_id && res.session_id !== sid) {
        localStorage.setItem(SID_KEY, res.session_id)
        setSid(res.session_id)
      }
      setAttachments((a) => [...a, { doc_id: res.doc_id, name: file.name }])
    } catch (e: any) {
      setMessages((m) => [...m, {
        role: 'assistant', isError: true, actions: [],
        content: `Couldn't upload "${file.name}" (${e?.message || e}).`,
      }])
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const applyChange = async (changeId: string) => {
    setBusy(true)
    try {
      const res = await applyPendingChange(sid, changeId)
      setPendingChanges((p) => p.filter((c) => c.change_id !== changeId))
      setMessages((m) => [...m, {
        role: 'assistant', actions: [],
        content: `Applied — revision ${res.revision}. It takes effect from the next call.`,
      }])
      refresh(['agents'])
    } catch (e: any) {
      // 409 = the agent changed underneath this proposal in a way that can't
      // be replayed, and the server has already dropped it. Clear the card too:
      // leaving it up invites the user to keep clicking an Apply that can never
      // succeed (it was clicked seven times in a row in production).
      const stale = e?.status === 409
      if (stale) setPendingChanges((p) => p.filter((c) => c.change_id !== changeId))
      setMessages((m) => [...m, {
        role: 'assistant', isError: true, actions: [],
        content: stale
          ? `That proposal is out of date — ${e?.message?.replace(/^409:\s*/, '') || 'the agent changed since it was proposed'} Ask me to redo it and I'll rebuild it from the current setup.`
          : `Couldn't apply the change (${e?.message || e}).`,
      }])
    } finally {
      setBusy(false)
    }
  }

  const discardChange = async (changeId: string) => {
    try {
      await discardPendingChange(sid, changeId)
    } catch { /* best-effort */ }
    setPendingChanges((p) => p.filter((c) => c.change_id !== changeId))
  }

  const selectChat = (id: string) => {
    if (id === sid || busy) return
    setLoadingTranscript(true)
    nearBottomRef.current = true
    setShowJump(false)
    localStorage.setItem(SID_KEY, id)
    setSid(id)
    setInput('')
  }

  const deleteChat = async (id: string) => {
    try {
      await copilotDeleteSession(id)
      setSessions((s) => s.filter((x) => x.session_id !== id))
      if (id === sid) newChat()
    } catch { /* best-effort */ }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const autoGrow = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  return (
    <div className="mx-auto flex h-[calc(100dvh-140px)] min-h-[420px] w-full max-w-6xl gap-5">
      <HistorySidebar
        sessions={sessions}
        activeSid={sid}
        onSelect={selectChat}
        onNew={newChat}
        onDelete={deleteChat}
      />

      {/* mobile chat-history sheet (the sidebar is hidden below md) */}
      <Sheet open={mobileChatsOpen} onOpenChange={setMobileChatsOpen}>
        <SheetContent
          side="left"
          aria-describedby={undefined}
          className="flex w-80 max-w-[85vw] flex-col gap-2 p-4 md:hidden"
        >
          <SheetHeader className="text-left">
            <SheetTitle className="font-display text-base font-bold tracking-tight">Previous chats</SheetTitle>
          </SheetHeader>
          <SessionList
            sessions={sessions}
            activeSid={sid}
            onSelect={(id) => { setMobileChatsOpen(false); selectChat(id) }}
            onNew={() => { setMobileChatsOpen(false); newChat() }}
            onDelete={deleteChat}
          />
        </SheetContent>
      </Sheet>

      <Sheet open={quickOpen} onOpenChange={setQuickOpen}>
        <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-md">
          <SheetHeader>
            <SheetTitle>Voice &amp; speed</SheetTitle>
          </SheetHeader>
          {quick && quick.target.kind !== 'none' && 'languages' in quick.state && (
            <div className="mt-4">
              <VoiceSpeedControls
                sid={sid}
                data={quick}
                onApplied={(res) => {
                  setQuick((q) => (q ? { ...q, target: res.target, state: res.state } : q))
                  if (res.interview) setInterview(res.interview)
                  if (res.pending_changes) setPendingChanges(res.pending_changes)
                  refresh(['agents'])
                }}
              />
            </div>
          )}
        </SheetContent>
      </Sheet>

      <div className="flex min-w-0 flex-1 flex-col">
      {/* header row */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          {messages.length > 0 && (
            <>
              <Sparkles size={15} className="shrink-0 text-mint-600 dark:text-mint-300" />
              <h1 className="truncate font-display text-base font-bold tracking-tight">Agent Copilot</h1>
              <span className="hidden truncate text-xs text-dim sm:inline">· builds and manages your voice agents</span>
            </>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {quick && quick.target.kind !== 'none' && (
            <Button
              variant="soft"
              size="sm"
              onClick={() => setQuickOpen(true)}
              title="Pick the agent's voice and speaking speed"
            >
              <SlidersHorizontal size={13} />
              <span className="hidden sm:inline">Voice &amp; speed</span>
              {'speaker' in quick.state && quick.state.speaker && (
                <span className="capitalize opacity-70">· {quick.state.speaker}</span>
              )}
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="md:hidden"
            onClick={() => setMobileChatsOpen(true)}
            title="Previous chats"
          >
            <MessagesSquare size={13} /> Chats
          </Button>
          {messages.length > 0 && (
            <Button variant="ghost" size="sm" onClick={newChat} title="Start a fresh conversation">
              <RotateCcw size={13} /> New chat
            </Button>
          )}
        </div>
      </div>

      {/* interview progress rail (v2) */}
      {interview && interview.phase !== 'idle' && messages.length > 0 && (
        <InterviewRail progress={interview} />
      )}

      {/* messages */}
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div ref={scrollRef} onScroll={onMessagesScroll} data-lenis-prevent className="flex flex-1 flex-col overflow-y-auto pr-1">
          {loadingTranscript ? (
            <div className="flex flex-col gap-4 py-2">
              <div className="skeleton h-16 w-2/3 rounded-2xl rounded-tl-md" />
              <div className="skeleton ml-auto h-10 w-1/2 rounded-2xl rounded-br-md" />
              <div className="skeleton h-20 w-3/4 rounded-2xl rounded-tl-md" />
            </div>
          ) : messages.length === 0 && !busy ? (
            <ChatEmpty onPick={(p) => send(p)} />
          ) : (
            <div className="flex flex-col gap-4 py-2">
              {messages.map((m, i) => (
                <Bubble
                  key={i}
                  msg={m}
                  onRetry={m.isError && i === messages.length - 1 && !busy ? retryLast : undefined}
                />
              ))}
              {pendingChanges.length > 0 && !busy && (
                <DiffCards
                  changes={pendingChanges}
                  busy={busy}
                  onApply={applyChange}
                  onDiscard={discardChange}
                />
              )}
              {busy && <TypingBubble />}
            </div>
          )}
        </div>
        {showJump && (
          <div className="absolute bottom-2 left-1/2 z-10 -translate-x-1/2">
            <Button variant="ghost" size="sm" onClick={jumpToLatest} title="Jump to the latest message">
              <ArrowDown size={13} /> Latest
            </Button>
          </div>
        )}
      </div>

      {/* composer */}
      <div className="mt-3">
        {messages.length > 0 && !tipDismissed && (
          <div className="mb-2">
            <PlaceholderTip
              onDismiss={() => { localStorage.setItem(TIP_KEY, '1'); setTipDismissed(true) }}
            />
          </div>
        )}
        {attachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {attachments.map((a) => (
              <Badge key={a.doc_id} tone="indigo">
                <FileText size={12} /> <span className="max-w-[40vw] truncate">{a.name}</span>
                <button
                  onClick={() => setAttachments((x) => x.filter((y) => y.doc_id !== a.doc_id))}
                  title="Remove attachment"
                  aria-label={`Remove ${a.name}`}
                  className="rounded-full p-0.5 hover:bg-indigo-400/20"
                >
                  <X size={11} />
                </button>
              </Badge>
            ))}
          </div>
        )}
        <div className="card flex items-end gap-2 p-2.5 transition-shadow focus-within:shadow-glow-mint focus-within:ring-1 focus-within:ring-mint-400/40">
          <input
            ref={fileRef}
            type="file"
            className="hidden"
            accept={DOC_ACCEPT}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void attachFile(f)
            }}
          />
          <IconButton
            label="Attach a brochure or price sheet — the copilot reads it into the agent"
            disabled={busy || uploading}
            onClick={() => fileRef.current?.click()}
          >
            {uploading ? <Spinner /> : <Paperclip size={16} />}
          </IconButton>
          {voice.supported && (
            <IconButton
              label={voice.state === 'recording'
                ? 'Stop recording and transcribe'
                : 'Dictate your message'}
              active={voice.state === 'recording'}
              disabled={busy || uploading || voice.state === 'transcribing'
                || voice.state === 'starting'}
              onClick={voice.toggle}
              className={voice.state === 'recording'
                ? 'bg-rose-500/12 text-rose-600 hover:bg-rose-500/20 dark:text-rose-300'
                : ''}
            >
              {voice.state === 'transcribing' || voice.state === 'starting'
                ? <Spinner />
                : voice.state === 'recording'
                  ? <Square size={13} className="fill-current" />
                  : <Mic size={16} />}
            </IconButton>
          )}
          <textarea
            ref={inputRef}
            rows={1}
            value={input}
            disabled={busy}
            aria-label="Message the copilot"
            placeholder={busy ? 'Copilot is working…' : 'Message the copilot — e.g. "Create a salon receptionist agent"'}
            onChange={(e) => { setInput(e.target.value); autoGrow(e.target) }}
            onKeyDown={onKeyDown}
            className="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed text-[var(--text)] outline-none placeholder:text-dim disabled:opacity-50"
          />
          <Button onClick={() => send()} disabled={busy || !input.trim()} title="Send (Enter)">
            {busy ? <Spinner /> : <SendHorizontal size={15} />}
          </Button>
        </div>
        {/* One status line under the composer: what the mic is doing, or the
            usual hint. Recording has to be unmistakable — a page that is
            listening and does not say so is the whole objection to mic
            buttons. */}
        {voice.state === 'recording' ? (
          <div className="mt-1.5 flex flex-wrap items-center justify-center gap-2 px-1 text-2xs">
            <span className="relative flex h-2 w-2" aria-hidden>
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-rose-500 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-rose-500" />
            </span>
            <span role="status" className="font-semibold text-rose-600 dark:text-rose-300">
              Listening… {voice.seconds.toFixed(1)}s
            </span>
            <span className="h-1.5 w-24 overflow-hidden rounded-full bg-[var(--surface-2)]" aria-hidden>
              <span className="block h-full rounded-full bg-mint-500"
                style={{ width: `${Math.min(100, Math.round(voice.level * 100))}%` }} />
            </span>
            <span className="text-dim">stops on its own · Esc to cancel</span>
          </div>
        ) : voice.state === 'transcribing' ? (
          <p className="mt-1.5 px-1 text-center text-2xs text-dim" role="status">
            <Spinner className="mr-1.5 inline h-3 w-3" /> Writing that down…
          </p>
        ) : (
          <p className="mt-1.5 px-1 text-center text-2xs text-dim">
            Enter to send · Shift+Enter for a new line
            {voice.supported ? ' · mic to dictate' : ''} · the copilot writes real agents under{' '}
            <code className="font-mono text-2xs">agents/</code>
          </p>
        )}
        {voice.error && (
          <p role="alert" className="mt-1 px-1 text-center text-2xs text-rose-600 dark:text-rose-300">
            {voice.error}
          </p>
        )}
        {/* What the corrector changed, and — for Hindi/Gujarati — the way out
            of the romanization. A silent rewrite of the user's own words is
            the one thing that would make dictation feel untrustworthy. */}
        {voice.last?.corrections?.length || voice.last?.romanized ? (
          <p className="mt-1 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 px-1 text-center text-2xs text-dim">
            {voice.last?.romanized && (
              <span className="inline-flex items-center gap-1.5">
                <Languages size={11} />
                {voice.last.language?.startsWith('gu') ? 'Gujarati' : 'Hindi'} → Hinglish
                <button type="button" onClick={useOriginalScript}
                  title="Put the original script in the box instead"
                  className="rounded px-1 py-0.5 font-semibold text-mint-700 underline underline-offset-2 hover:bg-[var(--surface-2)] dark:text-mint-300">
                  use {voice.last.language?.startsWith('gu') ? 'ગુજરાતી' : 'देवनागरी'}
                </button>
              </span>
            )}
            {voice.last?.corrections?.length ? (
              <span>
                heard it as{' '}
                {voice.last.corrections.slice(0, 3).map((c, i) => (
                  <span key={i}>
                    {i > 0 ? ', ' : ''}
                    <span className="line-through opacity-70">{c.from}</span> → {c.to}
                  </span>
                ))}
              </span>
            ) : null}
          </p>
        ) : null}
      </div>
      </div>

      {/* live spec preview pane (v2) — xl screens, while an interview is live */}
      {interview && interview.phase !== 'idle' &&
        Object.keys(interview.draft || {}).length > 0 && (
        <aside className="hidden w-64 shrink-0 flex-col gap-2 xl:flex">
          <Button
            variant="ghost"
            size="sm"
            className="justify-start"
            onClick={() => setShowPreview((v) => !v)}
            title={showPreview ? 'Hide the live preview' : 'Show the live preview'}
          >
            {showPreview ? <EyeOff size={13} /> : <Eye size={13} />}
            {showPreview ? 'Hide preview' : 'Show preview'}
          </Button>
          {showPreview && (
            <div className="flex-1 overflow-y-auto pr-0.5">
              <SpecPreview progress={interview} />
            </div>
          )}
        </aside>
      )}
    </div>
  )
}
