/**
 * Outbound Calls — the phone book and the dialler, on one page.
 *
 * The page has ONE audience vocabulary (lib/callStatus.ts): group × call
 * status × lead disposition. The campaign runner and the contact list render
 * the same three controls from it, and two buttons copy a selection between
 * them, so "what I am looking at" and "what I will dial" are always the same
 * sentence. They used to be two different lists — the list offered seven call
 * statuses, the runner offered three of them plus one disposition — which made
 * the audience you had just filtered to one you could not dial.
 *
 * Row selection is for ad-hoc subsets ("these eight people"); the runner is for
 * whole audiences. Both go through the same server-side planner, so do-not-call,
 * calling hours and the per-run cap apply either way.
 */
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown, ArrowUp, CalendarClock, ChevronDown, ExternalLink, FolderInput,
  Pencil, Phone, Play, Plus, RefreshCw, Square, Trash2, Upload, Users, X,
} from 'lucide-react'
import { post, patch, del, postForm } from '../api/mutate'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/confirm'
import { Checkbox } from '@/components/ui/checkbox'
import { Modal } from '../components/Modal'
import {
  Card, Badge, Button, EmptyState, IconButton, LiveDot, SectionHead,
  SkeletonBlock, Spinner, cx,
} from '../components/ui'
import {
  PageHeader, Table, THead, TH, TR, TD, StatusBadge, SearchInput, Pagination,
  Input, Select, Field, Segmented, AsyncState, ContactLabel,
} from '../components/kit'
import { StatCard } from '../components/StatCard'
import {
  ScheduleDialog, ScheduleList, useSchedules, type ScheduleTarget,
} from '../components/schedule'
import { useDebounced, useUrlNumber, useUrlParam, useUrlPatch } from '../lib/hooks'
import {
  CONTACT_STATUS_FILTER, DISPOSITION_OPTIONS, DISPOSITION_FILTER,
  DISPOSITION_AUDIENCE, STATUS_AUDIENCE, audienceLabel,
} from '../lib/callStatus'
import { num, fmtDateTime } from '../lib/format'

const PAGE_SIZE = 50

type Stats = {
  total: number; not_called: number; answered: number; empty_call: number
  no_answer: number; busy: number; declined: number; invalid_number?: number; failed: number
}
type Dispositions = {
  not_classified: number; interested: number; site_visit_booked: number
  inquiry: number; in_doubt: number; to_contact_later: number; rejected: number
}
type StatsResp = { stats?: Partial<Stats>; dispositions?: Partial<Dispositions> }

type Contact = {
  id: number | string
  db_id?: number
  phone: string
  name?: string | null
  gender?: string | null
  notes?: string | null
  group_id?: number | null
  added_at?: string | null
  call_status?: string | null
  disposition?: string | null
  call_count?: number | null
  last_called_at?: string | null
  call_notes?: string | null
  call_history?: { ts?: string; call_status?: string }[]
}
type ContactsResp = { contacts?: Contact[]; total?: number }

type Group = { id: number; name: string; count: number }
type GroupsResp = { groups?: Group[]; ungrouped?: number; total?: number }

type CampaignStatus = {
  running?: boolean; total?: number; completed?: number; answered?: number
  no_answer?: number; busy?: number; declined?: number; invalid_number?: number; failed?: number
  skipped?: number; dnd_skipped?: number; capped?: boolean
  current_phone?: string | null; current_name?: string | null
  started_at?: string | null; stopped?: boolean; error?: string | null
  max_calls_limit?: number; min_delay_seconds?: number
  audience?: string | null; audience_disposition?: string | null
  audience_group?: string | null; audience_size?: number
}

/** What the server says a start with the current form would actually dial. */
type Preview = {
  will_dial: number; matched: number; dnd_skipped: number; capped: boolean
  max_calls: number; max_calls_limit: number
}

type EditorState = {
  id: string; db_id?: number; name: string; phone: string
  gender: string; group_id: number | ''; notes: string
}

function CampaignTallies({ camp }: { camp: CampaignStatus }) {
  return (
    <div className="flex flex-wrap gap-2 text-xs">
      <Badge tone="mint">Answered {num(camp.answered)}</Badge>
      <Badge tone="amber">No answer {num(camp.no_answer)}</Badge>
      <Badge tone="amber">Busy {num(camp.busy)}</Badge>
      <Badge tone="rose">Declined {num(camp.declined)}</Badge>
      {!!camp.invalid_number && <Badge tone="rose">Invalid number {num(camp.invalid_number)}</Badge>}
      <Badge tone="rose">Failed {num(camp.failed)}</Badge>
      <Badge tone="neutral">Skipped {num(camp.skipped)}</Badge>
      {!!camp.dnd_skipped && <Badge tone="rose">Do not call {num(camp.dnd_skipped)}</Badge>}
    </div>
  )
}

function ProgressBar({ pct }: { pct: number }) {
  return (
    <div className="h-2 overflow-hidden rounded-full bg-[var(--surface-2)]">
      <div className="h-full rounded-full bg-gradient-to-r from-mint-300 to-mint-500 transition-all" style={{ width: `${pct}%` }} />
    </div>
  )
}

export default function Outbound() {
  const toast = useToast()
  const confirm = useConfirm()
  const nav = useNavigate()
  const qc = useQueryClient()
  const refresh = useRefresh()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['outbound-contacts'] })
    qc.invalidateQueries({ queryKey: ['outbound-stats'] })
    qc.invalidateQueries({ queryKey: ['contact-groups'] })
  }

  // View state — mirrored into the URL so refresh/back/share keep the view.
  const [group, setGroup] = useUrlParam('group', 'all')  // page-wide scope
  const [cs, setCs] = useUrlParam('cs', 'all')
  const [dp, setDp] = useUrlParam('dp', 'all')
  const [q, setQ] = useUrlParam('q')
  const [page, setPage] = useUrlNumber('page', 1)
  // urlPatch, not patch: `patch` in this file is the HTTP PATCH helper.
  const urlPatch = useUrlPatch()
  const dq = useDebounced(q, 300)

  // add contacts form
  const [showAdd, setShowAdd] = useState(false)
  const [phone, setPhone] = useState('')
  const [name, setName] = useState('')
  const [notes, setNotes] = useState('')
  const [adding, setAdding] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const addCardRef = useRef<HTMLDivElement>(null)
  const campaignRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const [fileName, setFileName] = useState('')
  const [uploading, setUploading] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  /** Where Add/Import puts contacts. '' = No group, 'new' = create one. */
  const [addGroup, setAddGroup] = useState<string>('')
  const [newGroupName, setNewGroupName] = useState('')

  // campaign form — an action, not a view, so it does not live in the URL
  const [campGroup, setCampGroup] = useState('all')
  const [campCs, setCampCs] = useState('not_called')
  const [campDp, setCampDp] = useState('all')
  const [delay, setDelay] = useState('4')
  const [maxCalls, setMaxCalls] = useState('0')
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)

  // row-level busy / expansion / selection
  const [dialingId, setDialingId] = useState<Contact['id'] | null>(null)
  const [deletingId, setDeletingId] = useState<Contact['id'] | null>(null)
  const [expandedId, setExpandedId] = useState<Contact['id'] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkBusy, setBulkBusy] = useState(false)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [savingEdit, setSavingEdit] = useState(false)
  const [moveOpen, setMoveOpen] = useState(false)
  const [moveTarget, setMoveTarget] = useState<string>('')
  const [moveNewName, setMoveNewName] = useState('')

  const campaignQ = useGet<CampaignStatus>(['campaign'], '/outbound/campaign/status', { refetchInterval: 2000 })
  const camp = campaignQ.data ?? {}
  const campRunning = !!camp.running

  const groupsQ = useGet<GroupsResp>(['contact-groups'], '/outbound/groups')
  const groups = groupsQ.data?.groups ?? []
  const groupName = (id?: number | string | null) => {
    if (id == null || id === '' || id === 'all') return 'All contacts'
    if (id === 'none') return 'No group'
    return groups.find((g) => String(g.id) === String(id))?.name ?? 'No group'
  }

  // While a campaign dials, keep statuses and stats ticking without a manual refresh.
  const statsQ = useGet<StatsResp>(['outbound-stats', group], `/outbound/stats?group=${group}`,
    { refetchInterval: campRunning ? 5000 : undefined })
  const contactsQ = useGet<ContactsResp>(
    ['outbound-contacts', group, cs, dp, dq, page],
    `/outbound/contacts?group=${group}&call_status=${cs}&disposition=${dp}`
    + `&q=${encodeURIComponent(dq)}&page=${page}&limit=${PAGE_SIZE}`,
    { refetchInterval: campRunning ? 5000 : undefined },
  )

  const stats = statsQ.data?.stats ?? {}
  const disp = statsQ.data?.dispositions ?? {}
  const contacts = Array.isArray(contactsQ.data?.contacts) ? contactsQ.data!.contacts! : []
  const listTotal = contactsQ.data?.total ?? contacts.length
  const pages = Math.max(1, Math.ceil(listTotal / PAGE_SIZE))
  const searching = contactsQ.isFetching && !contactsQ.isLoading
  const hasFilters = cs !== 'all' || dp !== 'all' || group !== 'all' || dq.trim() !== ''

  // Server-declared ceilings (defaults match dashboard/server.py) — stated in
  // the form so it can't promise an unlimited run the API would truncate.
  const maxLimit = Number(camp.max_calls_limit ?? 200)
  const minDelay = Number(camp.min_delay_seconds ?? 3)

  // Filter/scope changes reset the page. An effect, not a second URL-setter in
  // the same tick — those clobber each other (see useUrlPatch); the first run
  // is skipped so deep links keep ?page=.
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    if (page !== 1) setPage(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [group, cs, dp, dq])

  // A stale page (bookmarked link, or deleting the last rows of page 3) would
  // render blank forever — snap back once the real total says it cannot exist.
  useEffect(() => {
    if (contactsQ.data && page > pages) setPage(pages)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contactsQ.data, page, pages])

  /* ---------------------------------------------------------------- campaign preview */

  // The count under "Who to call" comes from the REAL planner, not from a stat
  // tally re-derived in the browser: the engine also drops do-not-call contacts
  // and applies the per-run cap, so a local count promised calls that were
  // never queued.
  const campaignBody = useMemo(() => ({
    call_status_filter: campCs,
    disposition_filter: campDp === 'all' ? '' : campDp,
    group: campGroup,
    delay_seconds: Number(delay) || 0,
    max_calls: Number(maxCalls) || 0,
  }), [campCs, campDp, campGroup, delay, maxCalls])

  // Scheduling. The dialog and the list are shared with Contacts; a scheduled
  // campaign carries the SAME body a Start would have sent, so what runs
  // tomorrow is what the preview above is describing now.
  const [scheduleTarget, setScheduleTarget] = useState<ScheduleTarget | null>(null)
  const schedulesQ = useSchedules()

  const [preview, setPreview] = useState<Preview | null>(null)
  const bodyKey = JSON.stringify(campaignBody)
  const statsAt = statsQ.dataUpdatedAt
  useEffect(() => {
    let alive = true
    post<Preview>('/outbound/campaign/preview', campaignBody)
      .then((r) => { if (alive) setPreview(r) })
      .catch(() => { if (alive) setPreview(null) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bodyKey, statsAt])

  /* ---------------------------------------------------------------- selection */

  const pageIds = contacts.map((c) => String(c.id))
  const allChecked = pageIds.length > 0 && pageIds.every((id) => selected.has(id))
  const toggleOne = (id: string) => setSelected((s) => {
    const n = new Set(s)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })
  const togglePage = () => setSelected((s) => {
    const n = new Set(s)
    if (allChecked) pageIds.forEach((id) => n.delete(id))
    else pageIds.forEach((id) => n.add(id))
    return n
  })
  const clearSelection = () => setSelected(new Set())

  /* ---------------------------------------------------------------- writes */

  /** Resolve the Add/Import group picker to what the API wants. */
  const addGroupPayload = (): { group_name?: string; group_id?: number | null } =>
    addGroup === 'new'
      ? { group_name: newGroupName.trim() }
      : { group_id: addGroup === '' ? null : Number(addGroup) }

  async function addContact() {
    if (!phone.trim()) { toast('Phone is required', 'warn'); return }
    if (addGroup === 'new' && !newGroupName.trim()) {
      toast('Name the new group first', 'warn'); return
    }
    setAdding(true)
    try {
      await post('/outbound/contacts', {
        phone: phone.trim(), name: name.trim(), notes: notes.trim(),
        ...addGroupPayload(),
      })
      const target = addGroup === 'new' ? newGroupName.trim()
        : addGroup === '' ? '' : groupName(addGroup)
      toast(`Contact added${target ? ` to ${target}` : ''}`, 'success')
      setPhone(''); setName(''); setNotes('')
      invalidate()
    } catch (e) {
      const msg = String((e as Error)?.message ?? e)
      toast(msg.includes('409') ? 'That number is already in your contacts' : `Failed: ${msg}`, 'error')
    } finally {
      setAdding(false)
    }
  }

  async function uploadFile() {
    const file = fileRef.current?.files?.[0]
    if (!file) { toast('Choose a file first', 'warn'); return }
    if (addGroup === 'new' && !newGroupName.trim()) {
      toast('Name the new group first', 'warn'); return
    }
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      // The group travels with the import, so "import my Marketing list" is one
      // action — every row used to land in No group with no way to re-file them
      // in bulk afterwards.
      const g = addGroupPayload()
      if (g.group_name) fd.append('group_name', g.group_name)
      if (g.group_id != null) fd.append('group_id', String(g.group_id))
      const r = await postForm<{
        ok?: boolean; added?: number; duplicates?: number; moved?: number
        parse_errors?: number; reason?: string
      }>('/outbound/contacts/upload', fd)
      if (r?.ok === false) {
        toast(r?.reason || 'Nothing imported', 'warn')
      } else {
        const bits = [`Added ${num(r?.added)} contacts`]
        if (r?.duplicates) bits.push(`${num(r.duplicates)} duplicates skipped`)
        if (r?.moved) bits.push(`${num(r.moved)} existing moved into the group`)
        if (r?.parse_errors) bits.push(`${num(r.parse_errors)} rows unreadable`)
        toast(bits.join(' · '), 'success')
      }
      if (fileRef.current) fileRef.current.value = ''
      setFileName('')
      invalidate()
    } catch (e) {
      toast(`Upload failed: ${String((e as Error)?.message ?? e)}`, 'error')
    } finally {
      setUploading(false)
    }
  }

  async function changeDisposition(id: Contact['id'], disposition: string) {
    // The toast system has no undo action, so writing-off a lead gets a
    // confirm BEFORE the patch instead of an after-the-fact undo.
    if (disposition === 'rejected') {
      const ok = await confirm({
        title: 'Mark as rejected?',
        description: 'This writes the lead off — campaigns and follow-ups will skip it.',
        confirmLabel: 'Mark rejected',
      })
      if (!ok) { invalidate(); return } // refetch snaps the <select> back
    }
    try {
      await patch(`/outbound/contacts/${id}`, { disposition })
      toast('Disposition updated', 'success')
      invalidate()
    } catch (e) {
      toast(`Failed: ${String((e as Error)?.message ?? e)}`, 'error')
    }
  }

  async function saveEdit() {
    if (!editor) return
    if (!editor.phone.trim()) { toast('Phone is required', 'warn'); return }
    setSavingEdit(true)
    try {
      await patch(`/outbound/contacts/${editor.id}`, {
        name: editor.name.trim(),
        phone: editor.phone.trim(),
        gender: editor.gender,
        notes: editor.notes.trim(),
        group_id: editor.group_id === '' ? null : editor.group_id,
      })
      toast('Contact updated', 'success')
      setEditor(null)
      invalidate()
    } catch (e) {
      const msg = String((e as Error)?.message ?? e)
      toast(msg.includes('409')
        ? 'Another contact already has that number'
        : `Save failed: ${msg}`, 'error')
    } finally {
      setSavingEdit(false)
    }
  }

  async function callContact(c: Contact) {
    const label = c.name ? `${c.name} (${c.phone})` : c.phone
    const ok = await confirm({
      title: 'Place call now?',
      description: `A real call will be placed to ${label} immediately.`,
      confirmLabel: 'Call now',
    })
    if (!ok) return
    setDialingId(c.id)
    try {
      const r = await post<{ status?: string }>(`/outbound/dial/${c.id}`, {})
      toast(`Dialing — ${r?.status ?? 'ok'}`, 'success')
      invalidate()
    } catch (e) {
      toast(`Dial failed: ${String((e as Error)?.message ?? e)}`, 'error')
    } finally {
      setDialingId(null)
    }
  }

  async function deleteContact(c: Contact) {
    const label = c.name ? `${c.name} (${c.phone})` : c.phone
    const ok = await confirm({
      title: 'Delete contact?',
      description: `${label} will be removed permanently. This cannot be undone.`,
      destructive: true,
    })
    if (!ok) return
    setDeletingId(c.id)
    try {
      await del(`/outbound/contacts/${c.id}`)
      toast('Deleted', 'success')
      setSelected((s) => { const n = new Set(s); n.delete(String(c.id)); return n })
      invalidate()
    } catch (e) {
      toast(`Failed: ${String((e as Error)?.message ?? e)}`, 'error')
    } finally {
      setDeletingId(null)
    }
  }

  async function clearAll() {
    const countLabel = stats.total != null ? `all ${num(stats.total)}` : 'ALL'
    // Say plainly that this ignores the group scope — the button sits under a
    // scoped list, so "Clear all" would otherwise read as "clear this group".
    const scope = group === 'all' ? ''
      : ` — this ignores the “${groupName(group)}” scope and deletes every contact you have`
    const ok = await confirm({
      title: 'Delete all contacts?',
      description: `This deletes ${countLabel} contacts permanently${scope}: call history, dispositions and notes included. This cannot be undone.`,
      destructive: true,
      confirmLabel: 'Delete all',
    })
    if (!ok) return
    try {
      await del('/outbound/contacts/all')
      toast('All contacts cleared', 'success')
      clearSelection()
      invalidate()
    } catch (e) {
      toast(`Failed: ${String((e as Error)?.message ?? e)}`, 'error')
    }
  }

  /* ---------------------------------------------------------------- bulk actions */

  async function bulk(action: 'delete' | 'disposition' | 'group', extra: Record<string, unknown> = {}) {
    setBulkBusy(true)
    try {
      const r = await post<{ matched?: number; changed?: number }>(
        '/outbound/contacts/bulk',
        { action, contact_ids: [...selected], ...extra })
      toast(`${num(r?.changed ?? r?.matched)} contact(s) updated`, 'success')
      clearSelection()
      invalidate()
    } catch (e) {
      toast(`Failed: ${String((e as Error)?.message ?? e)}`, 'error')
    } finally {
      setBulkBusy(false)
    }
  }

  async function bulkDelete() {
    const ok = await confirm({
      title: `Delete ${selected.size} contact(s)?`,
      description: 'They are removed permanently — call history, dispositions and notes included. This cannot be undone.',
      destructive: true,
      confirmLabel: `Delete ${selected.size}`,
    })
    if (ok) bulk('delete')
  }

  async function bulkMove() {
    if (moveTarget === 'new' && !moveNewName.trim()) {
      toast('Name the new group first', 'warn'); return
    }
    setMoveOpen(false)
    await bulk('group', moveTarget === 'new'
      ? { group_name: moveNewName.trim() }
      : { group_id: moveTarget === '' ? null : Number(moveTarget) })
    setMoveNewName('')
  }

  /** Dial exactly the ticked rows. Goes through the campaign engine, so the
   *  same do-not-call / calling-hours / pacing rules apply as a full run. */
  async function callSelected() {
    const ok = await confirm({
      title: `Call ${selected.size} selected contact(s)?`,
      description:
        `Each is dialled once, ${minDelay}s apart or more, up to ${num(maxLimit)} calls. `
        + 'Contacts marked Rejected are never dialled, and the run stops when calling hours end.',
      confirmLabel: 'Start calling',
    })
    if (!ok) return
    setStarting(true)
    try {
      const res = await post<{ total?: number; dnd_skipped?: number; capped?: boolean }>(
        '/outbound/campaign/start', {
          contact_ids: [...selected],
          delay_seconds: Number(delay) || 0,
          max_calls: Number(maxCalls) || 0,
        })
      const bits = [`Campaign started — ${num(res?.total)} queued`]
      if (res?.dnd_skipped) bits.push(`${num(res.dnd_skipped)} on do-not-call skipped`)
      if (res?.capped) bits.push(`capped at ${num(maxLimit)}`)
      toast(bits.join(' · '), 'success')
      clearSelection()
      qc.invalidateQueries({ queryKey: ['campaign'] })
      campaignRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    } catch (e) {
      toast(`Start failed: ${String((e as Error)?.message ?? e)}`, 'error')
    } finally {
      setStarting(false)
    }
  }

  /* ---------------------------------------------------------------- campaign */

  /** One sentence naming the audience, used by the confirm and the form. */
  const audienceDescription = [
    campCs === 'all' ? 'Everyone' : audienceLabel(campCs),
    campDp !== 'all' ? `· ${audienceLabel(campDp)}` : '',
    campGroup !== 'all' ? `· in ${groupName(campGroup)}` : '',
  ].filter(Boolean).join(' ')

  async function startCampaign() {
    // A campaign rings real phones in bulk, so the number gets said out loud
    // and confirmed before anything dials — the same human-in-the-loop rule
    // the follow-up dialer runs under. The number is the server's own plan.
    const willDial = preview?.will_dial ?? 0
    const ok = await confirm({
      title: `Call ${num(willDial)} ${willDial === 1 ? 'contact' : 'contacts'}?`,
      description:
        `Audience: ${audienceDescription}. Each contact is dialled once, ${minDelay}s apart `
        + `or more, up to ${num(maxLimit)} calls per campaign. Contacts marked Rejected are `
        + 'never dialled, and the run stops when calling hours end.',
      confirmLabel: 'Start calling',
    })
    if (!ok) return
    setStarting(true)
    try {
      const res = await post<{ total?: number; dnd_skipped?: number; capped?: boolean }>(
        '/outbound/campaign/start', campaignBody)
      const bits = [`Campaign started — ${num(res?.total)} queued`]
      if (res?.dnd_skipped) bits.push(`${num(res.dnd_skipped)} on do-not-call skipped`)
      if (res?.capped) bits.push(`capped at ${num(maxLimit)}`)
      toast(bits.join(' · '), 'success')
      qc.invalidateQueries({ queryKey: ['campaign'] })
    } catch (e) {
      toast(`Start failed: ${String((e as Error)?.message ?? e)}`, 'error')
    } finally {
      setStarting(false)
    }
  }

  async function stopCampaign() {
    setStopping(true)
    try {
      await post('/outbound/campaign/stop', {})
      toast('Campaign stopped', 'info')
      qc.invalidateQueries({ queryKey: ['campaign'] })
    } catch (e) {
      toast(`Stop failed: ${String((e as Error)?.message ?? e)}`, 'error')
    } finally {
      setStopping(false)
    }
  }

  /* ---------------------------------------------------------------- connectors */

  const filtersMatch = campGroup === group && campCs === cs && campDp === dp
  const matchListFilters = () => {
    setCampGroup(group); setCampCs(cs); setCampDp(dp)
    toast('Campaign audience set from the list filters', 'info')
    campaignRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  const showAudience = () => {
    urlPatch({ group: campGroup, cs: campCs, dp: campDp, page: null })
    listRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const dispBoxes = [
    { key: 'interested', label: 'Interested' },
    { key: 'site_visit_booked', label: 'Site visit booked' },
    { key: 'inquiry', label: 'Inquiry' },
    { key: 'in_doubt', label: 'In doubt' },
    { key: 'to_contact_later', label: 'To contact later' },
    { key: 'rejected', label: 'Rejected' },
  ] as const

  const statBoxes = [
    { key: 'all', label: 'Total', value: stats.total, tone: undefined },
    { key: 'not_called', label: 'Not called', value: stats.not_called, tone: undefined },
    { key: 'answered', label: 'Answered', value: stats.answered, tone: 'mint' },
    { key: 'empty_call', label: 'Empty call', value: stats.empty_call, tone: 'amber' },
    { key: 'no_answer', label: 'No answer', value: stats.no_answer, tone: 'amber' },
    { key: 'declined', label: 'Declined', value: stats.declined, tone: 'rose' },
    { key: 'invalid_number', label: 'Invalid number', value: stats.invalid_number ?? 0, tone: 'rose' },
    { key: 'busy', label: 'Busy', value: stats.busy, tone: 'amber' },
    { key: 'failed', label: 'Failed', value: stats.failed, tone: 'rose' },
  ] as const

  const total = Number(camp.total ?? 0)
  const completed = Number(camp.completed ?? 0)
  const progress = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0

  /** Group <option>s, shared by every group picker on the page. */
  const groupOptions = (opts: { all?: boolean; create?: boolean }) => (
    <>
      {opts.all && <option value="all">All contacts ({num(groupsQ.data?.total)})</option>}
      <option value={opts.all ? 'none' : ''}>No group ({num(groupsQ.data?.ungrouped)})</option>
      {groups.map((g) => (
        <option key={g.id} value={g.id}>{g.name} ({num(g.count)})</option>
      ))}
      {opts.create && <option value="new">+ New group…</option>}
    </>
  )

  return (
    <div className="space-y-6">
      <PageHeader
        title="Outbound Calls"
        sub="Contacts & calling campaigns"
        actions={
          <>
            {campRunning && (
              <Badge tone="mint"><LiveDot /> Campaign running</Badge>
            )}
            <Button variant="ghost" onClick={() => nav('/contacts')}>
              <Users size={14} /> Phone book
            </Button>
            <Button variant="ghost" onClick={() => refresh(['outbound-contacts'], ['outbound-stats'], ['campaign'], ['contact-groups'])}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </>
        }
      />

      {/* 0. Group scope — one control that narrows the whole page (stats,
             campaign audience and list), so "work on Marketing today" is a
             single choice rather than the same pick repeated three times. */}
      <Card className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
        <span className="eyebrow shrink-0">Group</span>
        <Select value={group} onChange={(e) => setGroup(e.target.value)}
          aria-label="Scope this page to one contact group" className="min-w-[200px]">
          {groupOptions({ all: true })}
        </Select>
        {group !== 'all' && (
          <Button variant="ghost" size="sm" onClick={() => setGroup('all')}>
            <X size={13} /> Clear scope
          </Button>
        )}
        <span className="text-xs text-dim">
          {group === 'all'
            ? 'Every figure and list below covers all your contacts.'
            : `Figures and the list below cover ${groupName(group)} only.`}
        </span>
        <div className="ml-auto">
          <Button variant="ghost" size="sm" onClick={() => nav('/contacts')}>
            Manage groups <ExternalLink size={12} />
          </Button>
        </div>
      </Card>

      {/* 1. Stat grid — call statuses. Each card filters the list: the numbers
             were read-only before, so a "Failed 12" card was a dead end. */}
      <div>
        <div className="eyebrow mb-2">Call status{group !== 'all' && ` · ${groupName(group)}`}</div>
        {statsQ.isLoading ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
            {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => <SkeletonBlock key={i} h={92} />)}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
            {statBoxes.map((b) => (
              <button key={b.key} type="button" onClick={() => setCs(b.key)}
                aria-pressed={cs === b.key}
                title={`Show ${b.label.toLowerCase()} contacts`}
                className={cx('rounded-2xl text-left transition',
                  cs === b.key && 'ring-2 ring-mint-400/60')}>
                <StatCard label={b.label} value={num(b.value)} iconTone={b.tone} />
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Lead dispositions */}
      <div>
        <div className="eyebrow mb-2">Lead dispositions{group !== 'all' && ` · ${groupName(group)}`}</div>
        {statsQ.isLoading ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            {[0, 1, 2, 3, 4, 5].map((i) => <SkeletonBlock key={i} h={92} />)}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            {dispBoxes.map((b) => (
              <button key={b.key} type="button" onClick={() => setDp(b.key)}
                aria-pressed={dp === b.key}
                title={`Show ${b.label.toLowerCase()} contacts`}
                className={cx('rounded-2xl text-left transition',
                  dp === b.key && 'ring-2 ring-mint-400/60')}>
                <StatCard label={b.label}
                  value={num((disp as Record<string, number | undefined>)[b.key])} />
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 2. Add Contacts (collapsible) */}
      <div ref={addCardRef}>
        <Card className="p-5">
          <button
            onClick={() => setShowAdd((v) => !v)}
            aria-expanded={showAdd}
            aria-controls="add-contacts-panel"
            className="flex w-full items-center justify-between gap-2 text-left">
            <span className="font-display text-lg font-bold tracking-tight">Add Contacts</span>
            <ChevronDown size={18} className={cx('text-dim transition-transform', showAdd && 'rotate-180')} />
          </button>

          {showAdd && (
            <div id="add-contacts-panel" className="mt-5 space-y-5 animate-fade-up">
              {/* One group target for BOTH paths — chosen once, applied to the
                  manual add and the file import alike. */}
              <div className="rounded-xl bg-[var(--surface-2)] p-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="text-xs font-semibold text-dim">Add to group</span>
                  <Select value={addGroup} onChange={(e) => setAddGroup(e.target.value)}
                    aria-label="Group new contacts are added to" className="min-w-[190px]">
                    {groupOptions({ create: true })}
                  </Select>
                  {addGroup === 'new' && (
                    <Input value={newGroupName} onChange={(e) => setNewGroupName(e.target.value)}
                      placeholder="New group name" aria-label="New group name" className="max-w-[220px]" />
                  )}
                  {group !== 'all' && group !== 'none' && addGroup !== group && (
                    <Button variant="ghost" size="sm" onClick={() => setAddGroup(group)}>
                      Use {groupName(group)}
                    </Button>
                  )}
                </div>
              </div>

              <div className="grid gap-6 lg:grid-cols-2">
                {/* manual */}
                <div className="space-y-3">
                  <div className="text-xs font-semibold text-dim">Add manually</div>
                  <Field label="Phone *">
                    <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+91…" />
                  </Field>
                  <Field label="Name">
                    <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Optional" />
                  </Field>
                  <Field label="Notes">
                    <Input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Optional" />
                  </Field>
                  <Button onClick={addContact} disabled={adding}>
                    {adding ? <Spinner /> : <Plus size={15} />} {adding ? 'Adding…' : 'Add'}
                  </Button>
                </div>

                {/* bulk */}
                <div className="space-y-3">
                  <div className="text-xs font-semibold text-dim">Bulk upload (.csv, .xlsx, .txt)</div>
                  <label
                    onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
                    onDragLeave={() => setDragOver(false)}
                    onDrop={(e) => {
                      e.preventDefault()
                      setDragOver(false)
                      if (e.dataTransfer.files.length && fileRef.current) {
                        fileRef.current.files = e.dataTransfer.files
                        setFileName(e.dataTransfer.files[0]?.name ?? '')
                      }
                    }}
                    className={cx(
                      'flex cursor-pointer flex-col items-center gap-2 rounded-xl border border-dashed border-[var(--border-strong)] bg-[var(--surface-2)] px-4 py-8 text-center transition hover:border-mint-400/50 hover:bg-mint-400/5',
                      dragOver && 'border-mint-400/60 bg-mint-400/5',
                    )}>
                    <Upload size={18} className="text-dim" />
                    <span className="text-sm font-medium text-soft">Click to browse or drop a file</span>
                    <span className="text-xs text-dim">CSV, XLSX or TXT — must include a phone column</span>
                    <input
                      ref={fileRef}
                      type="file"
                      accept=".csv,.xlsx,.xls,.txt"
                      className="sr-only"
                      onChange={(e) => setFileName(e.target.files?.[0]?.name ?? '')}
                    />
                  </label>
                  {fileName && (
                    <div><Badge tone="neutral"><span className="max-w-[70vw] truncate">{fileName}</span></Badge></div>
                  )}
                  <Button variant="soft" onClick={uploadFile} disabled={uploading}>
                    {uploading ? <Spinner /> : <Upload size={15} />} {uploading ? 'Uploading…' : 'Upload'}
                  </Button>
                </div>
              </div>
            </div>
          )}
        </Card>
      </div>

      {/* 3. Campaign Runner */}
      <div ref={campaignRef}>
        <Card className="p-5">
          <SectionHead title="Campaign Runner"
            sub={`Dials contacts one at a time inside calling hours. Contacts marked Rejected are never called; a run is capped at ${num(maxLimit)} calls.`}
            right={
              <Button variant="ghost" size="sm" onClick={matchListFilters} disabled={filtersMatch}
                title={filtersMatch ? 'Already matching the list below' : 'Copy the list filters into this audience'}>
                <ArrowDown size={13} /> Match list filters
              </Button>
            } />

          {/* The audience is the SAME three controls the list below uses. */}
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <Field label="Group">
              <Select value={campGroup} onChange={(e) => setCampGroup(e.target.value)}>
                {groupOptions({ all: true })}
              </Select>
            </Field>
            <Field label="Who to call">
              <Select value={campCs} onChange={(e) => setCampCs(e.target.value)}>
                {STATUS_AUDIENCE.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </Select>
            </Field>
            <Field label="Lead disposition">
              <Select value={campDp} onChange={(e) => setCampDp(e.target.value)}>
                {DISPOSITION_AUDIENCE.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </Select>
            </Field>
            <Field label="Delay (seconds)">
              <div>
                <Input type="number" min={minDelay} value={delay} onChange={(e) => setDelay(e.target.value)} />
                <div className="mt-1 text-xs text-dim tabular-nums">Minimum {minDelay}s between calls</div>
              </div>
            </Field>
            <Field label={`Max calls (0 = up to ${num(maxLimit)})`}>
              <div>
                <Input type="number" min={0} max={maxLimit} value={maxCalls} onChange={(e) => setMaxCalls(e.target.value)} />
                {Number(maxCalls) > maxLimit && (
                  <div className="mt-1 text-xs font-medium text-amber-600 dark:text-amber-300">
                    Capped at {num(maxLimit)} per campaign
                  </div>
                )}
              </div>
            </Field>
          </div>

          {/* The number the server itself would queue, not a local re-count. */}
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            {preview == null ? (
              <span className="text-dim">Checking audience…</span>
            ) : preview.will_dial > 0 ? (
              <>
                <span className="font-semibold tabular-nums text-[var(--text)]">
                  {num(preview.will_dial)} will be dialled
                </span>
                {preview.capped && (
                  <span className="text-amber-600 dark:text-amber-300">
                    of {num(preview.matched)} matching · capped at {num(preview.max_calls)}
                  </span>
                )}
                {!!preview.dnd_skipped && (
                  <span className="text-dim">{num(preview.dnd_skipped)} on do-not-call excluded</span>
                )}
                <span className="text-dim">· {audienceDescription}</span>
              </>
            ) : (
              <span className="font-medium text-amber-600 dark:text-amber-300">
                {preview.dnd_skipped
                  ? `Every contact in this audience is marked Rejected (${num(preview.dnd_skipped)}) — nothing to dial`
                  : 'No contacts match this audience — nothing to dial'}
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={showAudience}>
              <ArrowDown size={13} /> Show these contacts
            </Button>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            <Button onClick={startCampaign}
              disabled={campRunning || starting || !preview?.will_dial}
              title={!preview?.will_dial ? 'No contacts match the selected audience' : undefined}>
              {starting ? <Spinner /> : <Play size={15} />} {starting ? 'Starting…' : 'Start'}
            </Button>
            <Button variant="ghost" onClick={() => setScheduleTarget({
              kind: 'campaign', audience: campaignBody, summary: audienceDescription,
            })} disabled={!preview?.will_dial}
              title={!preview?.will_dial ? 'No contacts match the selected audience' : undefined}>
              <CalendarClock size={15} /> Schedule
            </Button>
            <Button variant="danger" onClick={stopCampaign} disabled={!campRunning || stopping}>
              {stopping ? <Spinner /> : <Square size={15} />} {stopping ? 'Stopping…' : 'Stop'}
            </Button>
          </div>

          {campRunning && (
            <div className="mt-5 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-sm">
                <div className="flex min-w-0 items-center gap-2 text-soft">
                  <LiveDot />
                  <span className="truncate font-semibold text-[var(--text)]">{camp.current_name || '—'}</span>
                  {camp.current_phone && <span className="text-dim tabular-nums">{camp.current_phone}</span>}
                </div>
                <div className="shrink-0 tabular-nums text-dim">{completed} / {total} · {progress}%</div>
              </div>
              <ProgressBar pct={progress} />
              <CampaignTallies camp={camp} />
            </div>
          )}

          {/* Last-run summary stays visible once the campaign ends */}
          {!campRunning && total > 0 && (
            <div className="mt-5 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="eyebrow">Last run</span>
                  {/* Which audience produced these tallies — the summary used
                      to be a row of numbers with no subject. */}
                  {camp.audience && (
                    <Badge tone="neutral">
                      {audienceLabel(camp.audience)}
                      {camp.audience_disposition ? ` · ${audienceLabel(camp.audience_disposition)}` : ''}
                      {camp.audience_group && camp.audience_group !== 'all'
                        ? ` · ${groupName(camp.audience_group)}` : ''}
                    </Badge>
                  )}
                  {camp.started_at && <span className="text-xs text-dim">started {fmtDateTime(camp.started_at)}</span>}
                </div>
                <div className="tabular-nums text-dim">{completed} / {total} completed</div>
              </div>
              <ProgressBar pct={progress} />
              <CampaignTallies camp={camp} />
            </div>
          )}

          {camp.error && (
            <div role="alert" className="mt-3 rounded-xl bg-rose-500/10 px-3 py-2 text-xs font-medium text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
              {camp.error}
            </div>
          )}
        </Card>
      </div>

      {/* 3b. What is queued for later. Directly under the runner, because this
             is the answer to "did my Schedule actually take?" and because a
             pending campaign here explains an audience that is about to be
             dialled without anyone pressing Start. */}
      <Card className="p-0">
        <SectionHead
          title="Scheduled"
          sub="Calls and campaigns waiting to run. Cancel any of them until they fire."
        />
        <div className="px-4 pb-4">
          <ScheduleList />
        </div>
      </Card>

      <ScheduleDialog
        target={scheduleTarget}
        onClose={() => setScheduleTarget(null)}
        hours={schedulesQ.data?.hours}
      />

      {/* 4. Contacts table (filters live inside the card) */}
      <div ref={listRef}>
        <Card className="p-5">
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <div className="max-w-full overflow-x-auto [scrollbar-width:none]">
              <Segmented
                options={CONTACT_STATUS_FILTER.map((o) => ({ value: o.value, label: o.label }))}
                value={cs}
                onChange={setCs}
              />
            </div>
            <Select value={dp} onChange={(e) => setDp(e.target.value)}
              aria-label="Filter by lead disposition">
              {DISPOSITION_FILTER.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </Select>
            <div className="relative">
              <SearchInput value={q} onChange={setQ} placeholder="Search phone or name…" className="w-56" />
              {searching && <Spinner className="absolute right-3 top-1/2 -translate-y-1/2" />}
            </div>
            <span className="text-xs tabular-nums text-dim">{num(listTotal)} contacts</span>
            <Button variant="ghost" size="sm" onClick={matchListFilters} disabled={filtersMatch}
              title={filtersMatch ? 'The campaign already targets this audience' : 'Aim the campaign runner at exactly this list'}>
              <ArrowUp size={13} /> Use as campaign audience
            </Button>
            <div className="ml-auto border-l border-[var(--border)] pl-3">
              <Button variant="danger" size="sm" onClick={clearAll}>
                <Trash2 size={14} /> Clear all{stats.total != null ? ` (${num(stats.total)})` : ''}
              </Button>
            </div>
          </div>

          {/* Bulk bar — present only when there is a selection to act on. */}
          {selected.size > 0 && (
            <div className="mb-4 flex flex-wrap items-center gap-2 rounded-xl bg-mint-400/10 px-3 py-2 ring-1 ring-mint-400/25">
              <span className="text-sm font-semibold tabular-nums text-[var(--text)]">
                {num(selected.size)} selected
              </span>
              <Button size="sm" onClick={callSelected} disabled={bulkBusy || starting || campRunning}
                title={campRunning ? 'A campaign is already running' : undefined}>
                {starting ? <Spinner /> : <Phone size={13} />} Call selected
              </Button>
              <Button variant="soft" size="sm" onClick={() => { setMoveTarget(''); setMoveOpen(true) }} disabled={bulkBusy}>
                <FolderInput size={13} /> Move to group
              </Button>
              <Select
                value=""
                aria-label="Set disposition for the selected contacts"
                disabled={bulkBusy}
                onChange={(e) => e.target.value && bulk('disposition', { disposition: e.target.value })}>
                <option value="">Set disposition…</option>
                {DISPOSITION_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </Select>
              <Button variant="danger" size="sm" onClick={bulkDelete} disabled={bulkBusy}>
                {bulkBusy ? <Spinner /> : <Trash2 size={13} />} Delete
              </Button>
              <Button variant="ghost" size="sm" onClick={clearSelection}>
                <X size={13} /> Clear
              </Button>
              <span className="ml-auto text-2xs text-dim">
                Ticking picks individual rows — to dial a whole audience, use the Campaign Runner.
              </span>
            </div>
          )}

          <AsyncState loading={contactsQ.isLoading} error={contactsQ.error}>
            {contacts.length === 0 ? (
              hasFilters ? (
                <EmptyState
                  icon={<Phone size={20} />}
                  title="No contacts match these filters"
                  hint={'Nothing is '
                    + (cs === 'all' ? '' : `${audienceLabel(cs).toLowerCase()} `)
                    + (dp === 'all' ? '' : `${cs === 'all' ? '' : 'and '}${audienceLabel(dp).toLowerCase()} `)
                    + (group === 'all' ? 'in your contacts' : `in ${groupName(group)}`)
                    + '. Try a different status, disposition, group or search term.'}
                  action={
                    <Button variant="ghost" onClick={() => urlPatch({ cs: null, dp: null, q: null, group: null, page: null })}>
                      Reset filters
                    </Button>
                  }
                />
              ) : (
                <EmptyState
                  icon={<Phone size={20} />}
                  title="No contacts yet"
                  hint="Add a contact manually or upload a CSV above — then run a campaign to dial through them."
                  action={
                    <Button onClick={() => { setShowAdd(true); addCardRef.current?.scrollIntoView({ behavior: 'smooth' }) }}>
                      <Plus size={15} /> Add contacts
                    </Button>
                  }
                />
              )
            ) : (
              <>
                <Table className={cx(searching && 'opacity-60 transition-opacity')}>
                  <THead>
                    <TH className="w-8">
                      <Checkbox aria-label="Select every contact on this page"
                        checked={allChecked} onCheckedChange={togglePage} />
                    </TH>
                    <TH>#</TH>
                    <TH>Contact</TH>
                    <TH>Group</TH>
                    <TH>Call Status</TH>
                    <TH>Disposition</TH>
                    <TH>Last Called</TH>
                    <TH>Calls</TH>
                    <TH className="text-right">Actions</TH>
                  </THead>
                  <tbody>
                    {contacts.map((c, i) => {
                      const rowId = String(c.id)
                      const isExpanded = expandedId === c.id
                      const isDialingNow = campRunning && !!c.phone && c.phone === camp.current_phone
                      return (
                        <Fragment key={c.id ?? i}>
                          <TR
                            onClick={() => setExpandedId(isExpanded ? null : c.id)}
                            className={cx(isDialingNow && 'bg-mint-400/10',
                              !isDialingNow && selected.has(rowId) && 'bg-mint-400/5')}>
                            <TD className="w-8">
                              <span onClick={(e) => e.stopPropagation()}
                                onKeyDown={(e) => e.stopPropagation()}>
                                <Checkbox
                                  aria-label={`Select ${c.name || c.phone}`}
                                  checked={selected.has(rowId)}
                                  onCheckedChange={() => toggleOne(rowId)} />
                              </span>
                            </TD>
                            <TD className="text-dim tabular-nums">
                              <span className="flex items-center gap-1.5">
                                <ChevronDown size={14} className={cx('shrink-0 text-dim transition-transform', isExpanded && 'rotate-180')} />
                                {(page - 1) * PAGE_SIZE + i + 1}
                              </span>
                            </TD>
                            <TD>
                              <span className="flex items-center gap-2">
                                <ContactLabel name={c.name} phone={c.phone} />
                                {isDialingNow && <StatusBadge value="dialing" />}
                              </span>
                            </TD>
                            <TD>
                              {c.group_id
                                ? <Badge tone="mint" soft>{groupName(c.group_id)}</Badge>
                                : <span className="text-xs text-dim">No group</span>}
                            </TD>
                            <TD><StatusBadge value={c.call_status} /></TD>
                            <TD>
                              <Select
                                value={c.disposition ?? 'not_classified'}
                                aria-label={`Disposition for ${c.name || c.phone}`}
                                onClick={(e) => e.stopPropagation()}
                                onChange={(e) => changeDisposition(c.id, e.target.value)}>
                                {DISPOSITION_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                              </Select>
                            </TD>
                            <TD className="text-dim" title={c.last_called_at ?? undefined}>{fmtDateTime(c.last_called_at)}</TD>
                            <TD className="tabular-nums">{num(c.call_count)}</TD>
                            <TD>
                              <div className="flex items-center justify-end gap-1.5" onClick={(e) => e.stopPropagation()}>
                                <IconButton label={`Call ${c.name || c.phone || 'contact'} now`}
                                  disabled={dialingId === c.id} onClick={() => callContact(c)}>
                                  {dialingId === c.id ? <Spinner /> : <Phone size={14} />}
                                </IconButton>
                                <IconButton
                                  label={`Schedule a call to ${c.name || c.phone || 'contact'}`}
                                  onClick={(e) => { e.stopPropagation(); setScheduleTarget({
                                    kind: 'call', contactId: String(c.id),
                                    name: c.name || undefined, phone: c.phone || undefined,
                                  }) }}>
                                  <CalendarClock size={14} />
                                </IconButton>
                                <IconButton label={`Edit ${c.name || c.phone || 'contact'}`}
                                  onClick={() => setEditor({
                                    id: rowId, db_id: c.db_id, name: c.name || '',
                                    phone: c.phone, gender: c.gender || '',
                                    group_id: c.group_id ?? '', notes: c.notes || '',
                                  })}>
                                  <Pencil size={14} />
                                </IconButton>
                                <IconButton label={`Delete ${c.name || c.phone || 'contact'}`}
                                  className="!text-rose-500 hover:!bg-rose-500/10"
                                  disabled={deletingId === c.id} onClick={() => deleteContact(c)}>
                                  {deletingId === c.id ? <Spinner /> : <Trash2 size={14} />}
                                </IconButton>
                              </div>
                            </TD>
                          </TR>
                          {isExpanded && (
                            <tr className="border-b border-[var(--border)]">
                              <TD colSpan={9} className="bg-[var(--surface-2)]">
                                <div className="sticky left-0 max-w-[calc(100vw-5rem)]">
                                  <div className="grid gap-4 py-1 sm:grid-cols-2 lg:grid-cols-4">
                                    <div>
                                      <div className="eyebrow mb-1">Notes</div>
                                      <div className="text-sm text-soft">{c.notes || '—'}</div>
                                    </div>
                                    <div>
                                      <div className="eyebrow mb-1">Call notes</div>
                                      <div className="text-sm text-soft">{c.call_notes || '—'}</div>
                                    </div>
                                    <div>
                                      <div className="eyebrow mb-1">Added</div>
                                      <div className="text-sm text-soft" title={c.added_at ?? undefined}>{fmtDateTime(c.added_at)}</div>
                                    </div>
                                    <div>
                                      <div className="eyebrow mb-1">Call history</div>
                                      {c.call_history?.length ? (
                                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                                          {c.call_history.map((h, hi) => (
                                            <span key={hi} className="inline-flex items-center gap-1.5">
                                              <StatusBadge value={h.call_status} />
                                              <span className="text-2xs text-dim">{fmtDateTime(h.ts)}</span>
                                            </span>
                                          ))}
                                        </div>
                                      ) : (
                                        <div className="text-sm text-dim">—</div>
                                      )}
                                    </div>
                                  </div>
                                  {c.db_id != null && (
                                    <div className="pt-3">
                                      <Button variant="ghost" size="sm" onClick={() => nav(`/contacts/${c.db_id}`)}>
                                        Open full contact <ExternalLink size={12} />
                                      </Button>
                                    </div>
                                  )}
                                </div>
                              </TD>
                            </tr>
                          )}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </Table>
                <Pagination page={Math.min(page, pages)} pages={pages} total={listTotal} onPage={setPage} />
              </>
            )}
          </AsyncState>
        </Card>
      </div>

      {/* Edit contact — the same fields the phone book edits, reachable without
          leaving the dialler. */}
      <Modal
        open={editor != null}
        onClose={() => setEditor(null)}
        title="Edit contact"
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditor(null)}>Cancel</Button>
            <Button onClick={saveEdit} disabled={savingEdit || !editor?.phone.trim()}>
              {savingEdit ? <Spinner /> : null} Save changes
            </Button>
          </>
        }>
        {editor && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Name">
                <Input value={editor.name} onChange={(e) => setEditor({ ...editor, name: e.target.value })}
                  placeholder="Full name" />
              </Field>
              <Field label="Phone *">
                <Input value={editor.phone} onChange={(e) => setEditor({ ...editor, phone: e.target.value })}
                  placeholder="+91 98765 43210" />
              </Field>
              <Field label="Gender">
                <Select value={editor.gender} onChange={(e) => setEditor({ ...editor, gender: e.target.value })}>
                  <option value="">—</option>
                  <option value="male">Male</option>
                  <option value="female">Female</option>
                  <option value="other">Other</option>
                </Select>
              </Field>
              <Field label="Group">
                <Select
                  value={editor.group_id === '' ? '' : String(editor.group_id)}
                  onChange={(e) => setEditor({ ...editor, group_id: e.target.value === '' ? '' : Number(e.target.value) })}>
                  {groupOptions({})}
                </Select>
              </Field>
            </div>
            <Field label="Notes">
              <Input value={editor.notes} onChange={(e) => setEditor({ ...editor, notes: e.target.value })}
                placeholder="Anything worth remembering" />
            </Field>
            <p className="text-xs text-dim">
              Correcting the number keeps this contact&apos;s call history. Imported
              detail columns are edited on the full contact page.
            </p>
            {editor.db_id != null && (
              <Button variant="ghost" size="sm" onClick={() => nav(`/contacts/${editor.db_id}`)}>
                Open full contact <ExternalLink size={12} />
              </Button>
            )}
          </div>
        )}
      </Modal>

      {/* Move the selection into a group */}
      <Modal
        open={moveOpen}
        onClose={() => setMoveOpen(false)}
        title={`Move ${selected.size} contact(s) to a group`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setMoveOpen(false)}>Cancel</Button>
            <Button onClick={bulkMove} disabled={bulkBusy}>
              {bulkBusy ? <Spinner /> : <FolderInput size={14} />} Move
            </Button>
          </>
        }>
        <div className="space-y-3">
          <Field label="Destination group">
            <Select value={moveTarget} onChange={(e) => setMoveTarget(e.target.value)} autoFocus>
              {groupOptions({ create: true })}
            </Select>
          </Field>
          {moveTarget === 'new' && (
            <Field label="New group name">
              <Input value={moveNewName} onChange={(e) => setMoveNewName(e.target.value)}
                placeholder="e.g. Marketing" />
            </Field>
          )}
          <p className="text-xs text-dim">
            A contact belongs to one group at a time — moving replaces whatever
            group it is in now.
          </p>
        </div>
      </Modal>
    </div>
  )
}
