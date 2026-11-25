/**
 * A simulated CALL with an agent — not a chat box.
 *
 * The engine is real: one ConversationRouter is opened server-side by
 * `POST /api/copilot/try/{slug}/start` and lives for the whole simulated
 * call, exactly as a phone call holds one. Every turn goes through the same
 * router, stage machine and slot filling a caller gets. Only the audio is
 * simulated, so no speech credits are spent.
 *
 * The agent speaks FIRST — that is what a call does, and it is why `start`
 * exists. The opening line lands before you type anything, and its answer to
 * your first word is the second beat (call_flow.connect.intro_line, or the
 * platform-composed identity line). Neither was observable while this screen
 * waited for the caller to open the conversation.
 *
 * The stage/slot readout is the point: you can watch the flow advance and the
 * slots fill turn by turn, without a production phone call being the first
 * execution of a freshly built agent.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, CornerDownLeft, PhoneOff, RotateCcw, User } from 'lucide-react'
import { post } from '../api/mutate'
import { Badge, Button, Spinner, cx } from './ui'
import { Textarea } from './kit'
import { FullscreenModal } from './Modal'

type Turn = { role: 'user' | 'assistant'; text: string; fillers?: string[] }
type Part = { text: string; intent: string; is_shortcut: boolean }
type TryResponse = {
  sim_id: string
  reply: string
  parts?: Part[]
  fillers?: string[]
  stage?: string
  language?: string
  slots?: Record<string, unknown>
  flags?: Record<string, unknown>
  bookings?: string[]
  turn?: number
  ended?: boolean
  end_reason?: string
  outcome?: string
  direction?: string
  agent_revision?: number
  model?: string
}

export function TryAgentDialog({
  slug, open, onOpenChange, direction,
}: {
  slug: string
  open: boolean
  onOpenChange: (v: boolean) => void
  /** Outbound agents get a contact name injected; inbound ones do not. */
  direction?: 'inbound' | 'outbound'
}) {
  const [simId, setSimId] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState('')
  const [expired, setExpired] = useState(false)
  const [meta, setMeta] = useState<TryResponse | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Answering the call: the agent's opening line arrives before the caller
  // says anything. Each open (and each Restart) is a brand-new call.
  const startCall = useCallback(async () => {
    setTurns([]); setDraft(''); setError(''); setMeta(null)
    setSimId(''); setExpired(false); setConnecting(true)
    try {
      const res = await post<TryResponse>(`/copilot/try/${slug}/start`, { direction })
      setSimId(res.sim_id)
      setMeta(res)
      if (res.reply) setTurns([{ role: 'assistant', text: res.reply }])
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setConnecting(false)
    }
  }, [slug, direction])

  useEffect(() => { if (open && slug) void startCall() }, [open, slug, startCall])

  // Pin to the newest turn by setting scrollTop on OUR container — never
  // scrollIntoView, which smooth-scrolls every scrollable ancestor including
  // the page behind the dialog.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [turns, busy, connecting])

  async function send() {
    const text = draft.trim()
    if (!text || busy || connecting || !simId || meta?.ended) return
    setTurns((t) => [...t, { role: 'user', text }])
    setDraft('')
    setBusy(true)
    setError('')
    try {
      const res = await post<TryResponse>(`/copilot/try/${slug}`, {
        sim_id: simId, message: text,
      })
      setMeta(res)
      setTurns((t) => [...t, {
        role: 'assistant',
        text: res.reply || '(the agent said nothing — it is waiting for you)',
        fillers: res.fillers,
      }])
    } catch (e: any) {
      // 410 = this simulated call is gone (idle timeout or a dashboard
      // restart); 409 = the agent already hung up. Both are "start a new
      // call", not "retry" — say so instead of showing a bare error.
      const status = e?.status as number | undefined
      if (status === 410 || status === 409) setExpired(true)
      setError(e?.reason || e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const slots = Object.entries(meta?.slots ?? {})
  const flags = Object.keys(meta?.flags ?? {})
  const ended = !!meta?.ended || expired
  const composerDisabled = busy || connecting || !simId || ended

  return (
    <FullscreenModal
      scope="shell"
      open={open}
      onClose={() => onOpenChange(false)}
      title={<span className="truncate">Call “{slug}”</span>}
      headerExtra={
        <div className="flex shrink-0 items-center gap-2">
          <Badge tone="amber">Call simulation</Badge>
          <Button variant="ghost" size="sm" onClick={() => void startCall()}
            disabled={busy || connecting}>
            <RotateCcw size={13} /> {ended ? 'Call again' : 'Restart'}
          </Button>
        </div>
      }
      footer={
        <div className="mx-auto w-full max-w-3xl">
          <div className="flex items-end gap-2">
            <Textarea
              rows={2}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
              }}
              aria-label="What the caller says"
              placeholder={ended
                ? 'The call has ended — Call again to start a new one'
                : 'e.g. Hi, I saw your ad about the 3BHK — what’s the price?'}
            />
            <Button onClick={send} disabled={composerDisabled || !draft.trim()}
              className="shrink-0">
              {busy ? <Spinner className="h-3.5 w-3.5" /> : <CornerDownLeft size={14} />} Send
            </Button>
          </div>
          <p className="mt-2 text-2xs text-dim">
            Enter sends · Shift+Enter for a new line
            {meta?.direction ? ` · ${meta.direction} call` : ''}
            {meta?.model ? ` · engine ${meta.model}` : ''}
            {meta?.agent_revision ? ` · rev ${meta.agent_revision}` : ''}
          </p>
        </div>
      }
    >
      {/* What this is, stated up front: the conversation logic is real, the
          phone call is not. Without this the transcript is easy to mistake for
          a recording of an actual call. */}
      <div className="shrink-0 border-b border-[var(--border)] px-4 py-2.5 sm:px-6">
        <p className="mx-auto max-w-3xl text-xs text-dim">
          This is a <span className="font-semibold text-soft">simulated call</span> — the agent
          answers first and every turn runs through the same live engine, stage machine and slot
          filling a real caller gets. Only the speech is skipped, so no voice credits are spent
          (and a booking made here never takes a real calendar slot).
        </p>
      </div>

      {/* Live state readout — the reason to test in text rather than by phone. */}
      {meta && (
        <div className="shrink-0 border-b border-[var(--border)] px-4 py-2.5 sm:px-6">
          <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-1.5">
            {meta.stage && <Badge tone="indigo">stage · {meta.stage}</Badge>}
            {meta.language && <Badge tone="neutral">{meta.language}</Badge>}
            {slots.length > 0
              ? slots.map(([k, v]) => (
                <Badge key={k} tone="mint">{k.replace(/_/g, ' ')}: {String(v)}</Badge>
              ))
              : <span className="text-xs text-dim">no slots filled yet</span>}
            {flags.map((f) => (
              <Badge key={f} tone="rose">{f.replace(/_/g, ' ')}</Badge>
            ))}
            {(meta.bookings ?? []).map((b) => (
              <Badge key={b} tone="amber">would book · {b}</Badge>
            ))}
          </div>
        </div>
      )}

      <div ref={scrollRef} data-lenis-prevent
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 sm:px-6">
        <div className="mx-auto max-w-3xl space-y-3">
          {connecting && (
            <p className="py-10 text-center text-sm text-dim">
              <Spinner className="mr-2 inline h-3.5 w-3.5" /> connecting…
            </p>
          )}
          {turns.map((t, i) => (
            <div key={i}>
              {/* Fillers are played on a real call while the model thinks,
                  and are deliberately kept out of the stored transcript —
                  shown here so a stall you would HEAR is not invisible. */}
              {(t.fillers ?? []).map((f, fi) => (
                <p key={fi} className="mb-1 pl-9 text-2xs italic text-dim">{f}</p>
              ))}
              <div className={cx('flex gap-2', t.role === 'user' && 'flex-row-reverse')}>
                <div className={cx('grid h-7 w-7 shrink-0 place-items-center rounded-full',
                  t.role === 'user' ? 'bg-[var(--surface-2)] text-soft' : 'bg-mint-400/15 text-mint-600 dark:text-mint-300')}>
                  {t.role === 'user' ? <User size={13} /> : <Bot size={13} />}
                </div>
                <div className={cx('max-w-[80%] whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-sm leading-relaxed',
                  t.role === 'user'
                    ? 'rounded-br-md bg-[var(--surface-2)] text-soft'
                    : 'rounded-bl-md bg-mint-400/10 text-[var(--text)] ring-1 ring-mint-400/20')}>
                  {t.text}
                </div>
              </div>
            </div>
          ))}
          {busy && (
            <div className="flex items-center gap-2 text-xs text-dim">
              <Spinner className="h-3.5 w-3.5" /> agent is thinking…
            </div>
          )}
          {meta?.ended && (
            <div className="flex items-center gap-2 pt-2 text-xs text-dim">
              <span className="h-px flex-1 bg-[var(--border)]" />
              <PhoneOff size={12} />
              Call ended{meta.end_reason ? ` — ${meta.end_reason}` : ''}
              <span className="h-px flex-1 bg-[var(--border)]" />
            </div>
          )}
          {error && (
            <div role="alert" className="rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
              {error}
              {expired && (
                <span className="ml-1">
                  Use <span className="font-semibold">Call again</span> to start a new one.
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </FullscreenModal>
  )
}
