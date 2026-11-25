/**
 * Activate an agent — the one place a number gets bound to an agent.
 *
 * "Activating" is the same operation wherever you start it, so it is one
 * module used in two places rather than two implementations that drift:
 *   · Build → Agents, from a card's Activate action
 *   · Copilot, right after the assistant creates or edits an agent
 *
 * It owns its own data (numbers) and its own mutations (PATCH /numbers/:id),
 * so a caller only supplies which agent to activate. That is deliberate: the
 * previous version lived inside AgentsList and depended on four pieces of that
 * page's state, which is why it could not be reused from chat.
 *
 * A number has TWO independent slots — inbound_agent_slug and
 * outbound_agent_slug — so the same number can be answered by one agent and
 * dial out with another. The slot is picked from the agent's own
 * call_direction; see slotFor.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle, ArrowRight, PhoneForwarded, Radio, Unplug,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { patch } from '../api/mutate'
import { Badge, Button, EmptyState, LiveDot, Spinner, cx } from './ui'
import { Modal } from './Modal'
import { useToast } from './Toast'

export type NumberRow = {
  id: number
  number: string
  label?: string | null
  inbound_agent_slug?: string | null
  outbound_agent_slug?: string | null
  config_status?: string | null
}

/* An agent binds to the slot matching its own call direction — an outbound
   agent speaks on outgoing calls, an inbound one answers incoming ones. */
export type BindSlot = 'inbound_agent_slug' | 'outbound_agent_slug'
export const slotFor = (dir?: string): BindSlot =>
  dir === 'outbound' ? 'outbound_agent_slug' : 'inbound_agent_slug'

export type ActivateTarget = {
  slug: string
  /** Display name; falls back to the slug. */
  title?: string
  /** The agent's own call_direction — decides which slot is bound. */
  direction?: string
}

export function ActivateAgentModal({
  target, open, onClose,
}: {
  target: ActivateTarget | null
  open: boolean
  onClose: () => void
}) {
  const { data } = useGet<{ numbers: NumberRow[] }>(['numbers'], '/numbers',
    { enabled: open })
  const refresh = useRefresh()
  const toast = useToast()
  const [busyId, setBusyId] = useState<number | null>(null)
  // A number already live with another agent needs an explicit detach confirm.
  const [confirmFor, setConfirmFor] = useState<NumberRow | null>(null)

  if (!open || !target) return null

  const numbers = data?.numbers ?? []
  const slug = target.slug
  const title = target.title || target.slug
  const slot = slotFor(target.direction)
  const outbound = slot === 'outbound_agent_slug'
  const role = outbound ? 'speaks on outgoing calls from' : 'answers incoming calls on'

  async function bind(n: NumberRow, value: string) {
    setBusyId(n.id)
    try {
      await patch(`/numbers/${n.id}`, { [slot]: value })
      toast(value
        ? `“${title}” is now live on ${n.number}`
        : `${n.number} disconnected — it has no ${outbound ? 'outbound' : 'inbound'} agent now`,
        value ? 'success' : 'info')
      refresh(['numbers'], ['agents'])
    } catch (e: any) {
      toast(`Could not ${value ? 'connect' : 'disconnect'}: ${e?.message || e}`, 'error')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Modal open onClose={onClose} title={`Activate “${title}”`} wide>
      <p className="mb-3 text-sm text-soft">
        An agent goes live by connecting it to one of your phone numbers — it then {role}{' '}
        that number.
      </p>

      {numbers.length === 0 ? (
        <EmptyState
          icon={<PhoneForwarded size={20} />}
          title="You have no numbers yet"
          hint="Connect your own number first — it takes about 2 minutes."
          action={
            <Link to="/telephony" onClick={onClose}>
              <Button variant="primary" size="sm">
                Go to Numbers (BYON) <ArrowRight size={13} />
              </Button>
            </Link>
          }
        />
      ) : (
        <div className="space-y-2">
          {numbers.map((n) => {
            const holder = n[slot]
            const mine = holder === slug
            const free = !holder
            const applied = n.config_status === 'applied'
            const busy = busyId === n.id
            return (
              <div key={n.id}
                className={cx('flex flex-wrap items-center gap-2.5 rounded-xl px-3.5 py-3 ring-1 transition',
                  mine ? 'bg-mint-400/10 ring-mint-400/30' : 'bg-[var(--surface-2)] ring-[var(--border)]')}>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold tabular-nums">{n.number}</span>
                    {!applied && (
                      <Badge tone="amber" soft>
                        <AlertTriangle size={11} /> setup incomplete
                      </Badge>
                    )}
                  </div>
                  {n.label && <div className="text-xs text-dim">{n.label}</div>}
                </div>
                <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
                  {mine ? (
                    <>
                      <Badge tone="mint"><LiveDot />Connected to this agent</Badge>
                      <Button variant="ghost" size="sm" disabled={busy}
                        onClick={() => bind(n, '')}>
                        {busy ? <Spinner className="h-3 w-3" /> : <Unplug size={13} />} Disconnect
                      </Button>
                    </>
                  ) : free ? (
                    <>
                      <Badge tone="neutral">Free</Badge>
                      <Button variant="primary" size="sm" disabled={busy}
                        onClick={() => bind(n, slug)}>
                        {busy ? <Spinner className="h-3 w-3" /> : <Radio size={13} />} Connect
                      </Button>
                    </>
                  ) : (
                    <>
                      <Badge tone="indigo">Live with “{holder}”</Badge>
                      <Button variant="soft" size="sm" disabled={busy}
                        onClick={() => setConfirmFor(n)}>
                        {busy ? <Spinner className="h-3 w-3" /> : <Radio size={13} />} Connect
                      </Button>
                    </>
                  )}
                </div>

                {confirmFor?.id === n.id && !mine && !free && (
                  <div role="alertdialog" className="mt-1 w-full rounded-xl bg-amber-400/10 px-3.5 py-3 ring-1 ring-amber-400/30">
                    <div className="flex items-start gap-2 text-sm text-soft">
                      <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-300" />
                      <span>
                        <b>{n.number}</b> currently {outbound ? 'calls out with' : 'is answered by'}{' '}
                        <b>“{holder}”</b>.
                        Detach it and connect <b>“{title}”</b> instead? The switch applies from the next call.
                      </span>
                    </div>
                    <div className="mt-2.5 flex flex-wrap items-center gap-2 pl-6">
                      <Button variant="danger" size="sm" disabled={busy}
                        onClick={() => { setConfirmFor(null); bind(n, slug) }}>
                        {busy ? <Spinner className="h-3 w-3" /> : <Unplug size={13} />} Detach &amp; connect
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setConfirmFor(null)}>Cancel</Button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
          <p className="pt-1 text-xs text-dim">
            “Setup incomplete” numbers can be connected now, but won't take calls until their
            trunk is applied in{' '}
            <Link to="/telephony" onClick={onClose}
              className="font-semibold text-indigo-600 hover:underline dark:text-indigo-300">
              Numbers (BYON)
            </Link>.
          </p>
        </div>
      )}
    </Modal>
  )
}
