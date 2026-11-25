import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  BookUser, CalendarClock, ChevronRight, FolderPlus, Folder, FolderOpen, Pencil,
  Plus, Trash2, Upload, Users, X,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { api } from '../api/client'
import { post, patch, del, postForm } from '../api/mutate'
import { Badge, Button, Card, EmptyState, SectionHead, Spinner, cx } from '../components/ui'
import { Checkbox } from '@/components/ui/checkbox'
import {
  PageHeader, Table, THead, TH, TR, TD, Pagination, SearchInput, Input,
  Textarea, Select, Field, AsyncState,
} from '../components/kit'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toast'
import { useConfirm, usePrompt } from '../components/confirm'
import {
  ScheduleDialog, ScheduleList, useSchedules, type ScheduleTarget,
} from '../components/schedule'
import { useDebounced, useUrlNumber, useUrlParam } from '../lib/hooks'
import { fmtDate } from '../lib/format'

type Contact = {
  id: string
  db_id?: number
  phone: string
  name: string
  notes: string
  gender?: string
  details?: Record<string, string>
  group_id?: number | null
  added_at?: string | null
  call_count?: number
  last_called_at?: string | null
}

type Group = { id: number; name: string; count: number }

type GroupsResp = { groups: Group[]; ungrouped: number; total: number }
type ContactsResp = { contacts: Contact[]; total: number }

const PAGE_SIZE = 50

function detailsPreview(d?: Record<string, string>): string {
  if (!d || Object.keys(d).length === 0) return ''
  return Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(' · ')
}

/** details ⇄ textarea: shown as "key: value" lines, parsed back to a dict. */
function detailsToText(d?: Record<string, string>): string {
  if (!d) return ''
  return Object.entries(d).map(([k, v]) => `${k}: ${v}`).join('\n')
}

function textToDetails(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  let free: string[] = []
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t) continue
    const i = t.indexOf(':')
    if (i > 0) out[t.slice(0, i).trim()] = t.slice(i + 1).trim()
    else free.push(t)
  }
  if (free.length) out.info = [out.info, ...free].filter(Boolean).join(' | ')
  return out
}

type EditorState = {
  mode: 'add' | 'edit'
  id?: string
  name: string
  phone: string
  gender: string
  group_id: number | ''
  notes: string
  detailsText: string
}

const EMPTY_EDITOR: EditorState = {
  mode: 'add', name: '', phone: '', gender: '', group_id: '', notes: '', detailsText: '',
}

export default function Contacts() {
  const toast = useToast()
  const nav = useNavigate()
  const refresh = useRefresh()
  const confirm = useConfirm()
  const prompt = usePrompt()
  // Group / search / page live in the URL so refresh & share keep the view.
  const [groupRaw, setGroupRaw] = useUrlParam('group', 'all')
  const groupSel: 'all' | 'none' | number =
    groupRaw === 'all' ? 'all' : groupRaw === 'none' ? 'none' : Number(groupRaw) || 'all'
  const setGroupSel = (v: 'all' | 'none' | number) => setGroupRaw(String(v))
  const [search, setSearch] = useUrlParam('search', '')
  const [page, setPage] = useUrlNumber('page', 1)
  const debSearch = useDebounced(search, 300)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [newGroupOpen, setNewGroupOpen] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  // Ticked rows, by the contact's string handle. Kept across pages so a
  // selection can be built up page by page; dropped when the view changes,
  // because "delete the selected" must never act on rows you can no longer see.
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkBusy, setBulkBusy] = useState(false)
  const [selectingAll, setSelectingAll] = useState(false)

  // Search/group changes reset the page (effect, not a second URL-setter call
  // in the same tick — those clobber each other); first run keeps deep links.
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    if (page !== 1) setPage(1)
    setSelected(new Set())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debSearch, groupRaw])

  // Scheduling shares one dialog and one list with Outbound — see
  // components/schedule.tsx for why this is not a per-page dialog.
  const [scheduleTarget, setScheduleTarget] = useState<ScheduleTarget | null>(null)
  const schedulesQ = useSchedules()

  const groupsQ = useGet<GroupsResp>(['contact-groups'], '/outbound/groups')
  const groups = groupsQ.data?.groups ?? []

  const groupParam = groupSel === 'all' ? 'all' : groupSel === 'none' ? 'none' : String(groupSel)
  // Server-side pagination: the page is part of the cache key so React Query
  // keeps each page separately (and `total` is the full filtered count, not
  // the size of the slice we were handed).
  const contactsQ = useGet<ContactsResp>(
    ['contacts-book', groupParam, debSearch, page],
    `/outbound/contacts?group=${groupParam}&q=${encodeURIComponent(debSearch || '')}&page=${page}&limit=${PAGE_SIZE}`,
  )
  const all = contactsQ.data?.contacts ?? []
  const total = contactsQ.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const rows = all

  // The server slices by the page we asked for, so a stale page number (bookmarked
  // deep link, or deleting the last contacts on page 3) would render a blank page
  // forever. Snap back the moment the real total says that page cannot exist.
  useEffect(() => {
    if (contactsQ.data && page > pages) setPage(pages)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contactsQ.data, page, pages])

  const groupName = (id?: number | null) => groups.find((g) => g.id === id)?.name

  const refreshAll = () => refresh(['contacts-book'], ['contact-groups'], ['outbound-contacts'])

  /* ---------------------------------------------------------------- selection */

  const pageIds = rows.map((c) => c.id)
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.has(id))
  const toggleOne = (id: string) => setSelected((s) => {
    const n = new Set(s)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })
  const togglePage = () => setSelected((s) => {
    const n = new Set(s)
    if (allOnPage) pageIds.forEach((id) => n.delete(id))
    else pageIds.forEach((id) => n.add(id))
    return n
  })
  const clearSelection = () => setSelected(new Set())
  const viewLabel = groupSel === 'all' ? 'all contacts'
    : groupSel === 'none' ? 'No group' : (groupName(groupSel) || 'this group')

  /** The page checkbox ticks one page; a group can run to many. This asks the
   *  server for every id the current group + search matches, so "select all"
   *  means all of them — the count in the confirm dialog is then the count
   *  that gets deleted. */
  const selectAllMatching = async () => {
    setSelectingAll(true)
    try {
      const r = await api<ContactsResp>(
        `/outbound/contacts?group=${groupParam}&q=${encodeURIComponent(debSearch || '')}`)
      setSelected(new Set((r.contacts ?? []).map((c) => c.id)))
    } catch (e: any) {
      toast(`Could not load the full list: ${e?.message || e}`, 'error')
    } finally {
      setSelectingAll(false)
    }
  }

  const bulkDelete = async () => {
    const n = selected.size
    if (!n) return
    if (!(await confirm({
      title: `Delete ${n.toLocaleString('en-IN')} contact${n === 1 ? '' : 's'}?`,
      description: 'They are removed permanently — call history, notes and details included. '
        + 'A later import of the same numbers starts them fresh. This cannot be undone.',
      destructive: true,
      confirmLabel: `Delete ${n.toLocaleString('en-IN')}`,
    }))) return
    setBulkBusy(true)
    try {
      const r = await post<{ matched?: number; changed?: number }>(
        '/outbound/contacts/bulk', { action: 'delete', contact_ids: [...selected] })
      toast(`${(r?.changed ?? r?.matched ?? n).toLocaleString('en-IN')} contact(s) deleted`, 'success')
      clearSelection()
      refreshAll()
    } catch (e: any) {
      toast(`Delete failed: ${e?.message || e}`, 'error')
    } finally {
      setBulkBusy(false)
    }
  }

  const saveContact = async () => {
    if (!editor) return
    setBusy(true)
    try {
      // Phone goes in BOTH directions. It used to be sent only on create, so
      // the edit dialog accepted a corrected number, said "Contact updated"
      // and kept the old one — a typo'd number was uneditable here, which is
      // the single most common reason to open this dialog at all. The server
      // has always accepted it (Outbound's copy of this dialog sends it).
      const body = {
        name: editor.name.trim(),
        phone: editor.phone.trim(),
        gender: editor.gender,
        notes: editor.notes.trim(),
        group_id: editor.group_id === '' ? null : editor.group_id,
        details: textToDetails(editor.detailsText),
      }
      if (editor.mode === 'add') {
        await post('/outbound/contacts', body)
        toast('Contact added', 'success')
      } else {
        await patch(`/outbound/contacts/${editor.id}`, body)
        toast('Contact updated', 'success')
      }
      setEditor(null)
      refreshAll()
    } catch (e: any) {
      toast(String(e?.message || e).includes('409') ? 'That phone number already exists in your contacts' : `Save failed: ${e?.message || e}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const removeContact = async (c: Contact) => {
    if (!(await confirm({
      title: 'Delete contact?',
      description: `${c.name || c.phone} will be removed from your contacts.`,
      destructive: true,
    }))) return
    try {
      await del(`/outbound/contacts/${c.id}`)
      toast('Contact deleted', 'success')
      refreshAll()
    } catch (e: any) {
      toast(`Delete failed: ${e?.message || e}`, 'error')
    }
  }

  const createGroup = async () => {
    const name = newGroupName.trim()
    if (!name) return
    setBusy(true)
    try {
      const r = await post<{ group?: { id: number; name: string } }>(
        '/outbound/groups', { name })
      toast(`Group “${name}” created — new contacts and imports go here now`, 'success')
      setNewGroupOpen(false)
      setNewGroupName('')
      // Select what you just made. Creating a group is always the first half of
      // "…and put these people in it", and leaving the sidebar on All contacts
      // meant the very next Add or Import silently landed in No group.
      if (r?.group?.id) setGroupSel(r.group.id)
      refresh(['contact-groups'])
    } catch (e: any) {
      toast(String(e?.message || e).includes('409') ? 'A group with that name already exists' : `Failed: ${e?.message || e}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const renameGroup = async (g: Group) => {
    const name = await prompt({
      title: `Rename “${g.name}”`,
      description: `Its ${g.count} contact(s) stay in it.`,
      initial: g.name,
      placeholder: 'Group name',
      confirmLabel: 'Rename',
      validate: (v) => (v.trim() ? null : 'Give the group a name'),
    })
    if (!name || name.trim() === g.name) return
    try {
      await patch(`/outbound/groups/${g.id}`, { name: name.trim() })
      toast('Group renamed', 'success')
      refreshAll()
    } catch (e: any) {
      toast(String(e?.message || e).includes('409')
        ? 'A group with that name already exists'
        : `Rename failed: ${e?.message || e}`, 'error')
    }
  }

  const removeGroup = async (g: Group) => {
    if (!(await confirm({
      title: `Delete group “${g.name}”?`,
      description: `Its ${g.count} contact(s) move to No group.`,
      destructive: true,
    }))) return
    try {
      await del(`/outbound/groups/${g.id}`)
      if (groupSel === g.id) setGroupSel('all')
      toast('Group deleted', 'success')
      refreshAll()
    } catch (e: any) {
      toast(`Delete failed: ${e?.message || e}`, 'error')
    }
  }

  const uploadFile = async (file: File) => {
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      // Import into the group you are standing in. Every import used to land in
      // No group regardless of the sidebar selection, so filling "Marketing"
      // from a spreadsheet meant re-filing every row by hand afterwards.
      if (typeof groupSel === 'number') form.append('group_id', String(groupSel))
      const r = await postForm<{
        ok: boolean; added?: number; duplicates?: number; moved?: number
        parse_errors?: number; reason?: string
      }>('/outbound/contacts/upload', form)
      if (r.ok) {
        const into = typeof groupSel === 'number' ? ` into ${groupName(groupSel)}` : ''
        toast(`Imported ${r.added} contact(s)${into}` +
          (r.duplicates ? `, ${r.duplicates} duplicate(s) skipped` : '') +
          (r.moved ? `, ${r.moved} existing moved in` : '') +
          (r.parse_errors ? `, ${r.parse_errors} row(s) unreadable` : ''),
          'success')
        refreshAll()
      } else {
        toast(r.reason || 'Nothing imported', 'warn')
      }
    } catch (e: any) {
      toast(`Upload failed: ${e?.message || e}`, 'error')
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  /** Sidebar row: a real <button> (keyboard operable) + optional rename/delete
   *  actions as siblings — buttons must not nest. */
  const sidebarItem = (
    active: boolean, label: string, count: number | undefined,
    onClick: () => void, icon: React.ReactNode,
    onDelete?: () => void, onRename?: () => void,
  ) => (
    <div
      key={label}
      className={cx(
        'group flex items-center rounded-xl text-sm font-medium transition',
        active ? 'bg-[var(--surface-2)] text-[var(--text)] ring-1 ring-[var(--border)]' : 'text-dim hover:bg-[var(--surface-2)] hover:text-soft',
      )}
    >
      <button
        type="button"
        onClick={onClick}
        aria-current={active ? 'true' : undefined}
        className="flex min-w-0 flex-1 items-center gap-2 rounded-xl px-3 py-2 text-left"
      >
        <span className="shrink-0">{icon}</span>
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {count != null && <span className="tabular-nums text-2xs text-dim">{count}</span>}
      </button>
      {onRename && (
        <button
          type="button"
          onClick={onRename}
          aria-label={`Rename group ${label}`}
          title="Rename group"
          className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-dim transition-opacity hover:text-soft lg:opacity-0 lg:group-focus-within:opacity-100 lg:group-hover:opacity-100"
        >
          <Pencil size={12} />
        </button>
      )}
      {onDelete && (
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Delete group ${label}`}
          title="Delete group"
          className="mr-1 grid h-7 w-7 shrink-0 place-items-center rounded-md text-dim transition-opacity hover:text-rose-400 lg:opacity-0 lg:group-focus-within:opacity-100 lg:group-hover:opacity-100"
        >
          <X size={13} />
        </button>
      )}
    </div>
  )

  return (
    <div className="space-y-6">
      <PageHeader
        title="Contacts"
        sub="Your phone book — people, groups & imported details"
        actions={
          <>
            <input
              ref={fileRef} type="file" accept=".csv,.xlsx,.xls,.txt" className="hidden"
              onChange={(e) => e.target.files?.[0] && uploadFile(e.target.files[0])}
            />
            <Button variant="ghost" onClick={() => fileRef.current?.click()} disabled={uploading}>
              {uploading ? <Spinner className="h-4 w-4" /> : <Upload size={14} />} Import CSV
            </Button>
            <Button onClick={() => { setEditor({ ...EMPTY_EDITOR, group_id: typeof groupSel === 'number' ? groupSel : '' }) }}>
              <Plus size={14} /> Add contact
            </Button>
          </>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[220px_1fr]">
        {/* Groups sidebar */}
        <Card className="h-fit p-3">
          <div className="mb-2 flex items-center justify-between px-1">
            <span className="eyebrow font-bold uppercase">Groups</span>
            <button
              type="button"
              onClick={() => setNewGroupOpen(true)}
              aria-label="New group" title="New group"
              className="grid h-9 w-9 -m-1.5 place-items-center rounded-md text-dim transition hover:bg-[var(--surface-2)] hover:text-soft lg:m-0 lg:h-6 lg:w-6"
            >
              <FolderPlus size={14} />
            </button>
          </div>
          <div className="space-y-0.5">
            {sidebarItem(groupSel === 'all', 'All contacts', groupsQ.data?.total, () => setGroupSel('all'), <Users size={14} />)}
            {sidebarItem(groupSel === 'none', 'No group', groupsQ.data?.ungrouped, () => setGroupSel('none'), <Folder size={14} />)}
            {groups.map((g) =>
              sidebarItem(groupSel === g.id, g.name, g.count, () => setGroupSel(g.id),
                groupSel === g.id ? <FolderOpen size={14} /> : <Folder size={14} />,
                () => removeGroup(g), () => renameGroup(g)),
            )}
          </div>
          {/* Where Add contact and Import CSV will actually put people. Both
              follow the sidebar selection, which is not obvious until an import
              lands somewhere you did not expect. */}
          <p className="mt-3 border-t border-[var(--border)] px-1 pt-3 text-2xs leading-snug text-dim">
            {typeof groupSel === 'number'
              ? <>New contacts and CSV imports go into <span className="font-semibold text-soft">{groupName(groupSel)}</span>.</>
              : 'New contacts and CSV imports start with no group — pick a group first to file them as they arrive.'}
          </p>
        </Card>

        {/* Contact list */}
        <Card className="p-5">
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <SearchInput value={search} onChange={setSearch} placeholder="Search name or number" className="w-full max-w-sm" />
            {contactsQ.isFetching && <Spinner className="h-4 w-4 shrink-0" />}
            <div className="ml-auto">
              {total > 0 && <Badge tone="neutral">{total.toLocaleString('en-IN')} contact{total === 1 ? '' : 's'}</Badge>}
            </div>
          </div>

          {/* Bulk bar — present only when there is a selection to act on. Deleting
              a group only un-files its members (they drop to No group); this is
              the way to remove the people themselves, one page or a whole group. */}
          {selected.size > 0 && (
            <div className="mb-4 flex flex-wrap items-center gap-2 rounded-xl bg-mint-400/10 px-3 py-2 ring-1 ring-mint-400/25">
              <span className="text-sm font-semibold tabular-nums text-[var(--text)]">
                {selected.size.toLocaleString('en-IN')} selected
              </span>
              {allOnPage && selected.size < total && (
                <Button variant="ghost" size="sm" onClick={selectAllMatching} disabled={selectingAll || bulkBusy}>
                  {selectingAll ? <Spinner className="h-3.5 w-3.5" /> : null}
                  Select all {total.toLocaleString('en-IN')} in {viewLabel}
                </Button>
              )}
              {selected.size >= total && total > pageIds.length && (
                <span className="text-xs text-dim">All {total.toLocaleString('en-IN')} in {viewLabel} are selected.</span>
              )}
              <Button variant="danger" size="sm" onClick={bulkDelete} disabled={bulkBusy || selectingAll}>
                {bulkBusy ? <Spinner className="h-3.5 w-3.5" /> : <Trash2 size={13} />} Delete
              </Button>
              <Button variant="ghost" size="sm" onClick={clearSelection} disabled={bulkBusy}>
                <X size={13} /> Clear
              </Button>
            </div>
          )}

          <AsyncState loading={contactsQ.isLoading} error={contactsQ.error}>
            {rows.length === 0 ? (
              <EmptyState
                icon={<BookUser size={20} />}
                title={search ? `No contacts match “${search}”` : 'No contacts here yet'}
                hint={search ? 'Try a shorter name or the last digits of the number.' : 'Add a person or import a CSV — name, phone and gender columns are picked up automatically; every other column is kept in Details.'}
                action={!search ? (
                  <Button size="sm" onClick={() => setEditor({ ...EMPTY_EDITOR })}><Plus size={14} /> Add contact</Button>
                ) : undefined}
              />
            ) : (
              <>
                <Table>
                  <THead>
                    <TH className="w-8">
                      <Checkbox aria-label="Select every contact on this page"
                        checked={allOnPage} onCheckedChange={togglePage} />
                    </TH>
                    <TH>Name</TH>
                    <TH>Phone</TH>
                    <TH>Gender</TH>
                    <TH>Group</TH>
                    <TH>Calls</TH>
                    <TH>Last called</TH>
                    <TH>Details</TH>
                    <TH className="text-right">Actions</TH>
                    <TH className="w-6"><span className="sr-only">Open</span></TH>
                  </THead>
                  <tbody>
                    {rows.map((c) => (
                      <TR key={c.id} className={cx('group', selected.has(c.id) && 'bg-mint-400/5')}
                        onClick={c.db_id ? () => nav(`/contacts/${c.db_id}`) : undefined}>
                        <TD className="w-8">
                          {/* The row itself opens the contact; the tick must not. */}
                          <span onClick={(e) => e.stopPropagation()}
                            onKeyDown={(e) => e.stopPropagation()}>
                            <Checkbox
                              aria-label={`Select ${c.name || c.phone}`}
                              checked={selected.has(c.id)}
                              onCheckedChange={() => toggleOne(c.id)} />
                          </span>
                        </TD>
                        <TD className="font-medium text-[var(--text)]">{c.name || <span className="text-dim">—</span>}</TD>
                        <TD className="whitespace-nowrap font-mono text-sm">{c.phone}</TD>
                        <TD>{c.gender ? <Badge tone={c.gender === 'female' ? 'rose' : c.gender === 'male' ? 'indigo' : 'neutral'} soft>{c.gender}</Badge> : <span className="text-dim">—</span>}</TD>
                        <TD>{groupName(c.group_id) ? <Badge tone="mint" soft>{groupName(c.group_id)}</Badge> : <span className="text-xs text-dim">No group</span>}</TD>
                        <TD className="tabular-nums">{c.call_count || <span className="text-dim">—</span>}</TD>
                        <TD className="whitespace-nowrap text-xs text-dim">
                          {fmtDate(c.last_called_at)}
                        </TD>
                        <TD className="max-w-[260px]">
                          <span className="block truncate text-xs text-dim" title={detailsPreview(c.details)}>
                            {detailsPreview(c.details) || (c.notes || '—')}
                          </span>
                        </TD>
                        <TD className="text-right">
                          <div className="flex items-center justify-end gap-1 transition-opacity lg:opacity-0 lg:focus-within:opacity-100 lg:group-hover:opacity-100">
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); setEditor({
                                mode: 'edit', id: c.id, name: c.name || '', phone: c.phone,
                                gender: c.gender || '', group_id: c.group_id ?? '',
                                notes: c.notes || '', detailsText: detailsToText(c.details),
                              }) }}
                              aria-label="Edit contact" title="Edit"
                              className="grid h-9 w-9 place-items-center rounded-md text-dim transition hover:bg-[var(--surface-2)] hover:text-soft lg:h-7 lg:w-7"
                            >
                              <Pencil size={14} />
                            </button>
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); setScheduleTarget({
                                kind: 'call', contactId: c.id, name: c.name, phone: c.phone,
                              }) }}
                              aria-label={`Schedule a call to ${c.name || c.phone}`}
                              title="Schedule a call"
                              className="grid h-9 w-9 place-items-center rounded-md text-dim transition hover:bg-[var(--surface-2)] hover:text-soft lg:h-7 lg:w-7"
                            >
                              <CalendarClock size={14} />
                            </button>
                            <button
                              type="button"
                              onClick={(e) => { e.stopPropagation(); removeContact(c) }}
                              aria-label="Delete contact" title="Delete"
                              className="grid h-9 w-9 place-items-center rounded-md text-dim transition hover:bg-[var(--surface-2)] hover:text-rose-400 lg:h-7 lg:w-7"
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </TD>
                        <TD className="w-6 pl-0 text-right">
                          {c.db_id ? (
                            <ChevronRight size={14} aria-hidden
                              className="text-dim transition-colors group-hover:text-soft" />
                          ) : null}
                        </TD>
                      </TR>
                    ))}
                  </tbody>
                </Table>
                <Pagination page={Math.min(page, pages)} pages={pages} total={total} onPage={setPage} />
              </>
            )}
          </AsyncState>
        </Card>

        {/* Scheduled calls. Shown here rather than only on Outbound because
            this is where an operator schedules one — a schedule you cannot see
            from the page that created it is a schedule you do not trust. */}
        <Card className="p-0">
          <SectionHead
            title="Scheduled calls"
            sub="Calls and campaigns waiting to run. Cancel any of them until they fire."
          />
          <div className="px-4 pb-4">
            <ScheduleList compact />
          </div>
        </Card>
      </div>

      <ScheduleDialog
        target={scheduleTarget}
        onClose={() => setScheduleTarget(null)}
        hours={schedulesQ.data?.hours}
      />

      {/* Add / edit contact */}
      <Modal
        open={editor != null}
        onClose={() => setEditor(null)}
        title={editor?.mode === 'edit' ? 'Edit contact' : 'Add contact'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditor(null)}>Cancel</Button>
            <Button onClick={saveContact} disabled={busy || !editor?.phone.trim()}>
              {busy ? <Spinner className="h-4 w-4" /> : null} {editor?.mode === 'edit' ? 'Save changes' : 'Add contact'}
            </Button>
          </>
        }
      >
        {editor && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Name">
                <Input value={editor.name} onChange={(e) => setEditor({ ...editor, name: e.target.value })} placeholder="Full name" />
              </Field>
              <Field label="Phone">
                <Input
                  value={editor.phone}
                  onChange={(e) => setEditor({ ...editor, phone: e.target.value })}
                  placeholder="+91 98765 43210"
                />
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
                  onChange={(e) => setEditor({ ...editor, group_id: e.target.value === '' ? '' : Number(e.target.value) })}
                >
                  <option value="">No group</option>
                  {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </Select>
              </Field>
            </div>
            <Field label="Notes">
              <Input value={editor.notes} onChange={(e) => setEditor({ ...editor, notes: e.target.value })} placeholder="Anything worth remembering" />
            </Field>
            <Field label="Details (one “key: value” per line)">
              <Textarea
                rows={4}
                value={editor.detailsText}
                onChange={(e) => setEditor({ ...editor, detailsText: e.target.value })}
                placeholder={'company: Pramukh Group\ncity: Ahmedabad'}
              />
            </Field>
          </div>
        )}
      </Modal>

      {/* New group */}
      <Modal
        open={newGroupOpen}
        onClose={() => { setNewGroupOpen(false); setNewGroupName('') }}
        title="New group"
        footer={
          <>
            <Button variant="ghost" onClick={() => { setNewGroupOpen(false); setNewGroupName('') }}>Cancel</Button>
            <Button onClick={createGroup} disabled={busy || !newGroupName.trim()}>
              {busy ? <Spinner className="h-4 w-4" /> : <FolderPlus size={14} />} Create group
            </Button>
          </>
        }
      >
        <Field label="Group name">
          <Input
            value={newGroupName}
            onChange={(e) => setNewGroupName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && createGroup()}
            placeholder="e.g. Site-visit leads"
            autoFocus
          />
        </Field>
      </Modal>
    </div>
  )
}
