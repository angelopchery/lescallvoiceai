import { Link } from 'react-router-dom'
import { MousePointerClick } from 'lucide-react'
import { Badge, Card } from '../../../components/ui'
import {
  AsyncState, ContactLabel, Pagination, Segmented, Table, TD, TH, THead, TR,
} from '../../../components/kit'
import { StatTile } from '../../../components/insight'
import { useGet } from '../../../api/hooks/useApi'
import { useUrlNumber, useUrlParam, useUrlPatch } from '../../../lib/hooks'
import { fmtDateTime } from '../../../lib/format'
import {
  waRepliesPath, waSendsPath, type WaOverview, type WaReply, type WaSend,
} from '../../../api/whatsapp'
import { WaStatusBadge } from './shared'

const FILTERS = {
  all: '',
  waiting: 'pending_approval,queued,sending',
  sent: 'sent,delivered,read',
  problems: 'failed,blocked,expired,skipped',
} as const
type Filter = keyof typeof FILTERS | 'replies'

const EMPTY: Record<Filter, string> = {
  all: 'Nothing yet. A message appears here as soon as a caller agrees on a call covered by a playbook.',
  waiting: 'Nothing waiting — every message has been sent or settled.',
  sent: 'No messages have gone out yet.',
  problems: 'No failed, blocked, skipped or expired messages.',
  replies: 'No replies yet. When a customer answers one of your messages, it shows here and in the bell.',
}

export function ActivityTab({ data }: { data: WaOverview }) {
  const [filterParam] = useUrlParam('show', 'all')
  const patchUrl = useUrlPatch()
  const filter = (filterParam in EMPTY ? filterParam : 'all') as Filter
  const [page, setPage] = useUrlNumber('page', 1)
  const c = data.counts
  const delivered = (c.delivered ?? 0) + (c.read ?? 0)
  const sent = (c.sent ?? 0) + delivered

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {([['Sent · 30 days', sent], ['Delivered', delivered], ['Read', c.read ?? 0],
           ['Link clicks', c.clicks ?? 0], ['Replies', c.replies ?? 0], ['Failed', c.failed ?? 0],
        ] as [string, number][]).map(([label, value]) => (
          <Card key={label} className="p-4"><StatTile label={label} value={value} /></Card>
        ))}
      </div>
      <Segmented value={filter} onChange={(v) => patchUrl({ show: v === 'all' ? null : v, page: null })}
        options={[{ value: 'all', label: 'All' }, { value: 'waiting', label: 'Waiting' },
                  { value: 'sent', label: 'Sent' }, { value: 'problems', label: 'Not sent' },
                  { value: 'replies', label: 'Replies' }]} />
      {filter === 'replies'
        ? <Replies page={page} setPage={setPage} />
        : <Sends status={FILTERS[filter]} filter={filter} page={page} setPage={setPage} />}
    </div>
  )
}

function Sends({ status, filter, page, setPage }: {
  status: string; filter: Filter; page: number; setPage: (p: number) => void
}) {
  const q = useGet<{ sends: WaSend[]; total: number; pages: number }>(
    ['wa', 'sends', status, page], waSendsPath(status, page), { refetchInterval: 30_000 })
  const rows = q.data?.sends ?? []
  return (
    <Card className="p-2">
      <AsyncState loading={q.isLoading} error={q.error} empty={rows.length === 0} emptyLabel={EMPTY[filter]}>
        <Table>
          <THead><TH>When</TH><TH>To</TH><TH>Playbook</TH><TH>Status</TH><TH>Detail</TH><TH>Call</TH></THead>
          <tbody>
            {rows.map((s) => (
              <TR key={s.id}>
                <TD className="whitespace-nowrap text-xs">{fmtDateTime(s.sent_at || s.created_at)}</TD>
                <TD><ContactLabel name={s.contact_name} phone={s.to_phone} /></TD>
                <TD className="text-xs">
                  <div className="font-medium text-[var(--text)]">{s.playbook_name || '—'}</div>
                  <div className="font-mono text-2xs text-dim">{s.template_name} · {s.language}</div>
                  {(s.project || s.media_filename) && (
                    <div className="text-2xs text-dim">{[s.project, s.media_filename].filter(Boolean).join(' · ')}</div>
                  )}
                </TD>
                <TD><WaStatusBadge status={s.status} /></TD>
                <TD className="max-w-xs text-xs">
                  <div className="flex flex-wrap items-center gap-1.5">
                    {s.clicks > 0 && <Badge tone="indigo"><MousePointerClick size={11} /> {s.clicks} click{s.clicks === 1 ? '' : 's'}</Badge>}
                    {s.pricing_category && <Badge tone="neutral">{s.pricing_category}</Badge>}
                  </div>
                  {(s.error || s.reason) && <div className="mt-1 line-clamp-2 text-dim" title={s.error || s.reason || ''}>{s.error || s.reason}</div>}
                </TD>
                <TD className="text-xs">
                  {s.call_key
                    ? <Link to={`/intelligence/${s.call_key}`} className="text-mint-600 hover:underline dark:text-mint-400">Open</Link>
                    : '—'}
                </TD>
              </TR>
            ))}
          </tbody>
        </Table>
      </AsyncState>
      <div className="px-2"><Pagination page={page} pages={q.data?.pages ?? 1} total={q.data?.total} onPage={setPage} /></div>
    </Card>
  )
}

function Replies({ page, setPage }: { page: number; setPage: (p: number) => void }) {
  const q = useGet<{ replies: WaReply[]; total: number; pages: number }>(
    ['wa', 'replies', page], waRepliesPath(page), { refetchInterval: 30_000 })
  const rows = q.data?.replies ?? []
  return (
    <Card className="p-2">
      <AsyncState loading={q.isLoading} error={q.error} empty={rows.length === 0} emptyLabel={EMPTY.replies}>
        <Table>
          <THead><TH>When</TH><TH>From</TH><TH>Message</TH></THead>
          <tbody>
            {rows.map((r) => (
              <TR key={r.id}>
                <TD className="whitespace-nowrap text-xs">{fmtDateTime(r.received_at)}</TD>
                <TD>
                  {r.contact_id
                    ? <Link to={`/contacts/${r.contact_id}`}><ContactLabel name={r.contact_name} phone={r.phone} /></Link>
                    : <ContactLabel name={r.contact_name} phone={r.phone} />}
                </TD>
                <TD className="text-sm">
                  {r.is_stop && <span className="mr-2"><Badge tone="rose">Opted out</Badge></span>}
                  <span className="text-[var(--text)]">{r.body}</span>
                </TD>
              </TR>
            ))}
          </tbody>
        </Table>
      </AsyncState>
      <div className="px-2"><Pagination page={page} pages={q.data?.pages ?? 1} total={q.data?.total} onPage={setPage} /></div>
      <p className="px-3 pb-2 text-2xs text-dim">Replying from the dashboard is not available yet — a reply here also rings the bell, and is on the contact’s timeline.</p>
    </Card>
  )
}
