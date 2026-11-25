/**
 * Integrations → WhatsApp.
 *
 * A caller agrees on the call ("haan, WhatsApp pe bhej do"); a PLAYBOOK says
 * what that agent sends — an approved template, the brochure, a tracked form
 * link — and whether it goes out on its own or waits for a person.
 *
 * Tabs (the active one lives in ?tab= so the bell's "waiting for approval"
 * link lands on the right one):
 *   Setup      the business number, platform readiness, sending rules
 *   Templates  Meta-approved messages, with a composer
 *   Playbooks  agent → message rules, with a live preview from a real call
 *   Approvals  messages waiting for a person
 *   Activity   everything sent, delivered, read, clicked, failed, and replies
 */
import { useEffect, useRef } from 'react'
import { AlertTriangle, FlaskConical } from 'lucide-react'
import { cx } from '../../components/ui'
import { AsyncState, PageHeader, Segmented } from '../../components/kit'
import { useGet } from '../../api/hooks/useApi'
import { useUrlParam, useUrlPatch } from '../../lib/hooks'
import { waOverviewPath, type WaOverview } from '../../api/whatsapp'
import { SetupTab, isConnected } from './whatsapp/SetupTab'
import { TemplatesTab } from './whatsapp/TemplatesTab'
import { PlaybooksTab } from './whatsapp/PlaybooksTab'
import { ApprovalsTab } from './whatsapp/ApprovalsTab'
import { ActivityTab } from './whatsapp/ActivityTab'
import { Hint } from './whatsapp/shared'

type Tab = 'setup' | 'templates' | 'playbooks' | 'approvals' | 'activity'
const TABS: Tab[] = ['setup', 'templates', 'playbooks', 'approvals', 'activity']

export default function WhatsApp() {
  const q = useGet<WaOverview>(['wa', 'overview'], waOverviewPath, { refetchInterval: 60_000 })
  const [tabParam] = useUrlParam('tab', '')
  const patchUrl = useUrlPatch()
  // A tab switch drops the other tabs' paging/filter so Activity page 4 does
  // not open Approvals on an empty page 4.
  const setTab = (v: string) => patchUrl({ tab: v, page: null, show: null })
  const data = q.data

  // With no ?tab=, open where the work is: nothing set up → Setup; messages
  // waiting → Approvals; otherwise the playbooks. Once, never fighting a click.
  const picked = useRef(false)
  useEffect(() => {
    if (picked.current || tabParam || !data) return
    picked.current = true
    const connected = isConnected(data.account)
    if ((data.counts.pending_approval_now ?? 0) > 0) setTab('approvals')
    else if (!connected && data.playbooks === 0) setTab('setup')
    else setTab('playbooks')
  }, [data, tabParam]) // eslint-disable-line react-hooks/exhaustive-deps
  const tab: Tab = (TABS as string[]).includes(tabParam) ? (tabParam as Tab) : 'setup'

  const pending = data?.counts.pending_approval_now ?? 0
  const connected = !!data && isConnected(data.account)

  return (
    <div>
      <PageHeader title="WhatsApp"
        sub="Send the brochure, form or details a caller agreed to on the call — automatically, or after you approve it."
        actions={data && (
          <span className="inline-flex items-center gap-2 text-sm">
            <span className={cx('inline-block h-2 w-2 rounded-full', connected ? 'bg-mint-500' : 'bg-[var(--border-strong)]')} />
            {connected ? (data.account.display_phone || (data.account.provider === 'interakt' ? 'Connected via Interakt' : 'Connected')) : 'Not connected'}
          </span>
        )} />

      {data && !data.platform.send_enabled && (
        <Hint tone="amber" className="mb-4 flex items-start gap-2">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span>
            Sending is switched off on the platform while WhatsApp is being rolled out. Playbooks,
            approvals and previews all work; approved messages wait in the queue and nothing
            reaches a customer until Namaskall switches sending on.
          </span>
        </Hint>
      )}
      {data?.platform.send_enabled && data.platform.canary && (
        <Hint tone="amber" className="mb-4 flex items-start gap-2">
          <FlaskConical size={15} className="mt-0.5 shrink-0" />
          <span>Test mode — only Namaskall’s test numbers receive messages. Everyone else’s are blocked, not queued.</span>
        </Hint>
      )}

      <div className="mb-5">
        <Segmented value={tab} onChange={(v) => setTab(v)}
          options={[
            { value: 'setup', label: 'Setup' },
            { value: 'templates', label: 'Templates' },
            { value: 'playbooks', label: 'Playbooks' },
            { value: 'approvals', label: pending ? `Approvals (${pending})` : 'Approvals' },
            { value: 'activity', label: 'Activity' },
          ]} />
      </div>

      <AsyncState loading={q.isLoading} error={q.error} variant="cards" rows={2}>
        {data && (
          <>
            {tab === 'setup' && <SetupTab data={data} />}
            {tab === 'templates' && <TemplatesTab data={data} />}
            {tab === 'playbooks' && <PlaybooksTab data={data} />}
            {tab === 'approvals' && <ApprovalsTab />}
            {tab === 'activity' && <ActivityTab data={data} />}
          </>
        )}
      </AsyncState>
    </div>
  )
}
