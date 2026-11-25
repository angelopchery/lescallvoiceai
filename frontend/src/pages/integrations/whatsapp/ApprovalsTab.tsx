import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, Clock, Pencil, PhoneCall, Quote, X } from 'lucide-react'
import { Badge, Button, Card, EmptyState } from '../../../components/ui'
import { AsyncState, ContactLabel, Input, Pagination } from '../../../components/kit'
import { Checkbox } from '../../../components/ui/checkbox'
import { useToast } from '../../../components/Toast'
import { useGet, useRefresh } from '../../../api/hooks/useApi'
import { useUrlNumber } from '../../../lib/hooks'
import { fmtDateTime } from '../../../lib/format'
import {
  approveSend, approveSends, editSend, skipSend, waSendsPath, type WaSend,
} from '../../../api/whatsapp'
import { Hint, MessageBubble, langLabel } from './shared'

export function ApprovalsTab() {
  const [page, setPage] = useUrlNumber('page', 1)
  const q = useGet<{ sends: WaSend[]; total: number; pages: number }>(
    ['wa', 'sends', 'pending', page], waSendsPath('pending_approval', page), { refetchInterval: 30_000 })
  const toast = useToast()
  const refresh = useRefresh()
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const rows = q.data?.sends ?? []
  const ready = rows.filter((s) => Object.values(s.params).every((v) => (v || '').trim()))

  async function bulk() {
    setBusy(true)
    try {
      const r = await approveSends([...picked])
      toast(`${r.approved.length} approved${r.failed.length ? ` · ${r.failed.length} need attention` : ''}`,
        r.failed.length ? 'warn' : 'success')
      setPicked(new Set())
      refresh(['wa'])
    } catch (e: any) { toast(e?.reason || e?.message, 'error') } finally { setBusy(false) }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-sm text-dim">
          Messages from manual-review playbooks wait here, each with the caller’s own words of
          agreement. Correct a name if the call misheard it, then approve.
        </p>
        {rows.length > 0 && (
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm"
              onClick={() => setPicked(picked.size === ready.length ? new Set() : new Set(ready.map((s) => s.id)))}>
              {picked.size === ready.length && ready.length ? 'Clear selection' : `Select all ready (${ready.length})`}
            </Button>
            <Button size="sm" disabled={!picked.size || busy} onClick={bulk}>
              <Check size={14} /> Approve selected ({picked.size})
            </Button>
          </div>
        )}
      </div>
      <AsyncState loading={q.isLoading} error={q.error} variant="cards" rows={2} empty={false}>
        {rows.length === 0 ? (
          <Card>
            <EmptyState icon={<Check size={20} />} title="Nothing waiting for approval"
              hint="When a caller agrees on a call and the agent’s playbook is set to manual review, the message appears here." />
          </Card>
        ) : (
          <div className="space-y-3">
            {rows.map((s) => (
              <PendingCard key={s.id} s={s} checked={picked.has(s.id)}
                onCheck={(v) => {
                  const next = new Set(picked)
                  if (v) next.add(s.id); else next.delete(s.id)
                  setPicked(next)
                }} />
            ))}
          </div>
        )}
      </AsyncState>
      <Pagination page={page} pages={q.data?.pages ?? 1} total={q.data?.total} onPage={setPage} />
    </div>
  )
}

function PendingCard({ s, checked, onCheck }: { s: WaSend; checked: boolean; onCheck: (v: boolean) => void }) {
  const toast = useToast()
  const refresh = useRefresh()
  const [editing, setEditing] = useState(false)
  const [values, setValues] = useState<Record<string, string>>(s.params)
  const [busy, setBusy] = useState(false)
  const empty = Object.entries(s.params).filter(([, v]) => !(v || '').trim()).map(([k]) => k)
  const who = s.contact_name || s.to_phone

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true)
    try {
      await fn()
      toast(ok, 'success')
      refresh(['wa'])
    } catch (e: any) { toast(e?.reason || e?.message, 'error') } finally { setBusy(false) }
  }

  return (
    <Card className="p-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-3">
          <div className="flex items-start gap-3">
            <Checkbox className="mt-1" checked={checked} disabled={empty.length > 0}
              onCheckedChange={(v) => onCheck(!!v)} aria-label={`Select the message to ${who}`} />
            <div className="min-w-0 flex-1">
              <ContactLabel name={s.contact_name} phone={s.to_phone} />
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-dim">
                <span>{s.playbook_name}</span>
                {s.agent_slug && <span>{s.agent_slug}</span>}
                {s.project && <span>{s.project}</span>}
                <span>{langLabel(s.language)}</span>
                {s.call_key && (
                  <Link to={`/intelligence/${s.call_key}`} className="inline-flex items-center gap-1 text-mint-600 hover:underline dark:text-mint-400">
                    <PhoneCall size={11} /> Call {fmtDateTime(s.created_at)}
                  </Link>
                )}
                {s.expires_at && <span className="inline-flex items-center gap-1"><Clock size={11} /> Expires {fmtDateTime(s.expires_at)}</span>}
              </div>
            </div>
          </div>
          {s.consent_quote ? (
            <div className="flex gap-2 rounded-xl bg-mint-400/10 px-3 py-2 text-sm ring-1 ring-mint-400/20">
              <Quote size={14} className="mt-0.5 shrink-0 text-mint-600 dark:text-mint-400" />
              <div>
                <div className="text-2xs font-semibold uppercase tracking-wide text-mint-700 dark:text-mint-300">{s.consent_label || 'Consent'}</div>
                <div className="text-soft">“{s.consent_quote}”</div>
              </div>
            </div>
          ) : (
            <Badge tone="neutral">{s.consent_label || 'Agreed on the call'}</Badge>
          )}
          {s.reason && <Hint tone="amber">{s.reason}</Hint>}
          {(editing || empty.length > 0) && (
            <div className="space-y-2">
              {/* A link button's value is the tracked-link token we minted —
                  not something a person should retype. */}
              {Object.keys(s.params).filter((k) => !k.startsWith('button.') || !(s.params[k] || '').trim()).map((k) => (
                <div key={k} className="flex items-center gap-2">
                  <span className="w-36 shrink-0 truncate font-mono text-2xs text-dim" title={k}>{k.replace(/^(body|header|button)\./, '')}</span>
                  <Input value={values[k] ?? ''} onChange={(e) => setValues({ ...values, [k]: e.target.value })} />
                </div>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={busy || Object.values(values).some((v) => !(v || '').trim())}
              onClick={() => act(() => approveSend(s.id, editing || empty.length ? values : undefined), `Approved — sending to ${who}`)}>
              <Check size={14} /> Approve
            </Button>
            {!editing && empty.length === 0 && (
              <Button size="sm" variant="ghost" onClick={() => setEditing(true)}><Pencil size={13} /> Edit values</Button>
            )}
            {editing && (
              <Button size="sm" variant="ghost" disabled={busy}
                onClick={() => act(async () => { await editSend(s.id, values); setEditing(false) }, 'Saved')}>
                Save without approving
              </Button>
            )}
            <Button size="sm" variant="danger" disabled={busy}
              onClick={() => act(() => skipSend(s.id), `Skipped — nothing will be sent to ${who}`)}>
              <X size={14} /> Skip
            </Button>
          </div>
        </div>
        <MessageBubble preview={s.preview} attachment={s.media_filename}
          empty="This template is not synced for this language yet." />
      </div>
    </Card>
  )
}
