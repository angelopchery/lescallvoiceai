import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Bot, FileText, FileUp, Link2, Pencil, Plus, ShieldCheck, Trash2, Zap,
} from 'lucide-react'
import { Badge, Button, Card, EmptyState, IconButton, cx } from '../../../components/ui'
import { AsyncState, Field, Input, MultiSelect, Segmented, Select } from '../../../components/kit'
import { FullscreenModal } from '../../../components/Modal'
import { Switch } from '../../../components/ui/switch'
import { useToast } from '../../../components/Toast'
import { useConfirm } from '../../../components/confirm'
import { useGet, useRefresh } from '../../../api/hooks/useApi'
import type { AgentsList } from '../../../api/agents'
import {
  createPlaybook, deletePlaybook, previewPlaybook, updatePlaybook, uploadWaMedia,
  waMediaPath, waPlaybooksPath, waProjectsPath, waTemplatesPath,
  type WaMedia, type WaOverview, type WaPlaybook, type WaPlaybookDraft,
  type WaPlaybookPreview, type WaProject, type WaTemplate,
} from '../../../api/whatsapp'
import { useDebounced } from '../../../lib/hooks'
import { Hint, MessageBubble, TemplateStatusBadge, TokenChips, langLabel } from './shared'

type Draft = WaPlaybookDraft

function blank(approval: 'manual' | 'auto'): Draft {
  return {
    name: '', enabled: false, agent_slugs: [], require_booking: false, recipient: 'dialled',
    template_name: null, variables: {}, media_id: null, media_map: {}, link_url: null,
    approval_mode: approval,
  }
}

/* Two starting points matching the two things agents promise on calls. */
const STARTERS: { key: string; title: string; text: string; make: (a: 'manual' | 'auto') => Draft }[] = [
  {
    key: 'brochure', title: 'Brochure after the call',
    text: 'The caller agreed to receive details — send the brochure of the project they discussed, with their name on it.',
    make: (a) => ({ ...blank(a), name: 'Brochure after the call', approval_mode: 'auto' }),
  },
  {
    key: 'form', title: 'Form link after the call',
    text: 'The caller agreed to fill a form — send a Google Form link, pre-filled with what the call collected.',
    make: (a) => ({
      ...blank(a), name: 'Form link after the call',
      link_url: 'https://docs.google.com/forms/d/e/FORM_ID/viewform?usp=pp_url&entry.1111111111={contact_name}',
    }),
  },
]

const KNOWN = new Set(['greeting', 'contact_name', 'first_name', 'title', 'honorific', 'project_name',
  'visit_date', 'visit_time', 'agent_name', 'company_name', 'link'])

/** Sensible starting values when a template is picked: a body variable named
 *  like one of our placeholders maps to it, and {greeting} never goes out empty.
 *  Interakt templates number their variables ({{1}}, {{2}}…); the first is
 *  almost always the greeting, so it starts as one. The rest are the author's. */
const POSITIONAL_GUESS = ['{greeting}', '{agent_name}', '{project_name}']
function defaultVariables(t: WaTemplate | undefined, current: Draft['variables']): Draft['variables'] {
  if (!t) return current
  const out: Draft['variables'] = {}
  let n = 0
  for (const s of t.slots) {
    if (s.kind === 'document' || s.kind === 'image' || s.kind === 'video') continue
    if (current[s.key]) { out[s.key] = current[s.key]; continue }
    const name = s.key.split('.')[1]
    const positional = /^\d+$/.test(name) && s.key.startsWith('body.')
    const guess = positional ? POSITIONAL_GUESS[n++] ?? '' : ''
    out[s.key] = {
      value: KNOWN.has(name) ? `{${name}}` : guess,
      fallback: name === 'greeting' || name === 'contact_name' || name === 'first_name' || guess === '{greeting}' ? 'there'
        : guess === '{agent_name}' ? 'our team' : '',
    }
  }
  return out
}

export function PlaybooksTab({ data }: { data: WaOverview }) {
  const q = useGet<{ playbooks: WaPlaybook[] }>(['wa', 'playbooks'], waPlaybooksPath)
  const toast = useToast()
  const confirm = useConfirm()
  const refresh = useRefresh()
  const [editing, setEditing] = useState<{ id: number | null; draft: Draft } | null>(null)
  const approval = data.account.default_approval

  async function toggle(pb: WaPlaybook, enabled: boolean) {
    try {
      await updatePlaybook(pb.id, { ...strip(pb), enabled })
      toast(enabled ? `“${pb.name}” is on` : `“${pb.name}” is off`, 'success')
      refresh(['wa'])
    } catch (e: any) {
      toast(e?.reason || e?.message, 'error')
    }
  }

  const list = q.data?.playbooks ?? []
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-sm text-dim">
          A playbook says what an agent sends when a caller agrees on the call — the template, the
          attachment or link, and whether a person approves it first.
        </p>
        <Button size="sm" onClick={() => setEditing({ id: null, draft: blank(approval) })}>
          <Plus size={14} /> New playbook
        </Button>
      </div>
      <AsyncState loading={q.isLoading} error={q.error} variant="cards" rows={2}
        empty={false}>
        {list.length === 0 ? (
          <Card className="p-5">
            <EmptyState icon={<Zap size={20} />} title="No playbooks yet"
              hint="Start from one of these — both match what agents already promise on calls." />
            <div className="mx-auto grid max-w-3xl gap-3 sm:grid-cols-2">
              {STARTERS.map((s) => (
                <button key={s.key} type="button" onClick={() => setEditing({ id: null, draft: s.make(approval) })}
                  className="rounded-2xl bg-[var(--surface-2)] p-4 text-left ring-1 ring-[var(--border)] transition hover:ring-mint-400/40">
                  <div className="text-sm font-semibold">{s.title}</div>
                  <div className="mt-1 text-xs text-dim">{s.text}</div>
                </button>
              ))}
            </div>
          </Card>
        ) : (
          <div className="grid gap-4 lg:grid-cols-2">
            {list.map((pb) => (
              <Card key={pb.id} className="p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate font-semibold">{pb.name}</div>
                    <div className="mt-1 flex flex-wrap gap-1.5">
                      <Badge tone={pb.approval_mode === 'auto' ? 'indigo' : 'amber'}>
                        {pb.approval_mode === 'auto' ? <><Zap size={11} /> Automatic</> : <><ShieldCheck size={11} /> Manual review</>}
                      </Badge>
                      {pb.template_name && <Badge tone="neutral">{pb.template_name}</Badge>}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <Switch checked={pb.enabled} onCheckedChange={(v) => toggle(pb, v)}
                      aria-label={`${pb.enabled ? 'Turn off' : 'Turn on'} ${pb.name}`} />
                    <IconButton label={`Edit ${pb.name}`} onClick={() => setEditing({ id: pb.id, draft: strip(pb) })}>
                      <Pencil size={15} />
                    </IconButton>
                    <IconButton label={`Delete ${pb.name}`} onClick={async () => {
                      if (await confirm({
                        title: `Delete “${pb.name}”?`,
                        description: 'Messages from it that are still waiting are skipped. Sent history stays in Activity.',
                        destructive: true,
                      })) {
                        await deletePlaybook(pb.id).catch((e) => toast(e?.message, 'error'))
                        refresh(['wa'])
                      }
                    }}><Trash2 size={15} /></IconButton>
                  </div>
                </div>
                <dl className="mt-3 space-y-1.5 text-xs">
                  <Row icon={<Bot size={13} />} label="Agents">
                    {pb.agent_slugs.length ? pb.agent_slugs.join(', ') : <span className="text-amber-600 dark:text-amber-300">none picked</span>}
                  </Row>
                  {(pb.media_filename || Object.keys(pb.media_map ?? {}).length > 0) && (
                    <Row icon={<FileText size={13} />} label="Attachment">
                      {Object.keys(pb.media_map ?? {}).length > 0
                        ? `${Object.keys(pb.media_map).length} project brochure${Object.keys(pb.media_map).length === 1 ? '' : 's'}${pb.media_filename ? ` · default ${pb.media_filename}` : ''}`
                        : pb.media_filename}
                    </Row>
                  )}
                  {pb.link_url && <Row icon={<Link2 size={13} />} label="Link"><span className="break-all">{pb.link_url}</span></Row>}
                </dl>
                <div className="mt-3 flex gap-3 border-t border-[var(--border)] pt-2 text-2xs text-dim">
                  <span>{pb.sends_total ?? 0} message{pb.sends_total === 1 ? '' : 's'} created</span>
                  {!!pb.sends_pending && (
                    <Link to="/integrations/whatsapp?tab=approvals" className="font-semibold text-amber-600 hover:underline dark:text-amber-300">
                      {pb.sends_pending} awaiting approval
                    </Link>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </AsyncState>
      {editing && (
        <PlaybookEditor id={editing.id} initial={editing.draft} onClose={() => setEditing(null)} />
      )}
    </div>
  )
}

function Row({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2">
      <dt className="flex w-24 shrink-0 items-center gap-1.5 text-dim">{icon}{label}</dt>
      <dd className="min-w-0 text-soft">{children}</dd>
    </div>
  )
}

function strip(pb: WaPlaybook): Draft {
  return {
    name: pb.name, enabled: pb.enabled, agent_slugs: pb.agent_slugs, require_booking: pb.require_booking,
    recipient: pb.recipient, template_name: pb.template_name, variables: pb.variables,
    media_id: pb.media_id, media_map: pb.media_map ?? {}, link_url: pb.link_url,
    approval_mode: pb.approval_mode,
  }
}

/* ------------------------------------------------------------------ editor */

function PlaybookEditor({ id, initial, onClose }: { id: number | null; initial: Draft; onClose: () => void }) {
  const toast = useToast()
  const refresh = useRefresh()
  const [d, setD] = useState<Draft>(initial)
  const [busy, setBusy] = useState(false)
  const agents = useGet<AgentsList>(['agents'], '/agents')
  const tq = useGet<{ templates: WaTemplate[] }>(['wa', 'templates'], waTemplatesPath)
  const mq = useGet<{ media: WaMedia[] }>(['wa', 'media'], waMediaPath)
  const fileRef = useRef<HTMLInputElement>(null)

  const byName = useMemo(() => {
    const m = new Map<string, WaTemplate[]>()
    for (const t of tq.data?.templates ?? []) m.set(t.name, [...(m.get(t.name) ?? []), t])
    return m
  }, [tq.data])
  const variants = d.template_name ? byName.get(d.template_name) ?? [] : []
  const tpl = variants.find((v) => (v.status || '').toUpperCase() === 'APPROVED') ?? variants[0]
  const textSlots = (tpl?.slots ?? []).filter((s) => s.kind === 'text' || s.kind === 'url_suffix')
  const needsMedia = !!tpl?.media
  // The projects the chosen agents pitch — one brochure each.
  const pq = useGet<{ projects: WaProject[] }>(['wa', 'projects', d.agent_slugs.join(',')],
    waProjectsPath(d.agent_slugs), { enabled: needsMedia && d.agent_slugs.length > 0 })
  const projectNames = useMemo(() => {
    const names = (pq.data?.projects ?? []).map((p) => p.name)
    for (const k of Object.keys(d.media_map ?? {})) if (!names.includes(k)) names.push(k)
    return names
  }, [pq.data, d.media_map])
  const [projectUpload, setProjectUpload] = useState<string | null>(null)

  // Live preview against a real call of the chosen agent.
  const debounced = useDebounced(JSON.stringify(d), 500)
  const [pv, setPv] = useState<WaPlaybookPreview | null>(null)
  const [pvError, setPvError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    const draft = JSON.parse(debounced) as Draft
    if (!draft.template_name) { setPv(null); return }
    previewPlaybook({ ...draft, enabled: false })
      .then((r) => { if (live) { setPv(r); setPvError(null) } })
      .catch((e) => { if (live) setPvError(e?.reason || e?.message) })
    return () => { live = false }
  }, [debounced])

  const agentOptions = (agents.data?.agents ?? []).map((a) => a.name)
  const canEnable = d.agent_slugs.length > 0 && !!d.template_name

  async function save() {
    setBusy(true)
    try {
      const body = { ...d, name: d.name.trim() || 'Untitled playbook', enabled: d.enabled && canEnable }
      if (id == null) await createPlaybook(body)
      else await updatePlaybook(id, body)
      toast('Playbook saved', 'success')
      refresh(['wa'])
      onClose()
    } catch (e: any) {
      toast(e?.reason || e?.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  function setVar(key: string, field: 'value' | 'fallback', v: string) {
    const cur = d.variables[key] ?? { value: '', fallback: '' }
    setD({ ...d, variables: { ...d.variables, [key]: { ...cur, [field]: v } } })
  }

  return (
    <FullscreenModal open onClose={onClose} scope="shell"
      title={id == null ? 'New playbook' : `Edit “${initial.name}”`}
      footer={<div className="flex w-full flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={d.enabled && canEnable} disabled={!canEnable}
            onCheckedChange={(v) => setD({ ...d, enabled: v })} />
          {canEnable ? (d.enabled ? 'On — sends for new calls' : 'Off') : 'Pick an agent and a template to switch it on'}
        </label>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button disabled={busy} onClick={save}>Save playbook</Button>
        </div>
      </div>}>
      {/* The fullscreen modal gives its children the space between a fixed
          header and footer; this body is the part that scrolls. */}
      <div data-lenis-prevent className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6">
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-5">
          <Field label="Name">
            <Input value={d.name} maxLength={80} onChange={(e) => setD({ ...d, name: e.target.value })}
              placeholder="e.g. Swarit brochure" />
          </Field>

          <Section title="When">
            <Field label="Agents">
              <MultiSelect className="w-full" label="Agents" allLabel="Pick agents…"
                summary={(n) => `${n} agents`} options={agentOptions} value={d.agent_slugs}
                onChange={(v) => setD({ ...d, agent_slugs: v })} />
            </Field>
            <div className="space-y-2 text-sm">
              <label className="flex items-start gap-2 text-soft">
                <input type="checkbox" checked disabled className="mt-1" />
                <span>The caller agreed on the call to receive it on WhatsApp <span className="text-dim">(always required)</span></span>
              </label>
              <label className="flex items-start gap-2 text-soft">
                <input type="checkbox" className="mt-1" checked={d.require_booking}
                  onChange={(e) => setD({ ...d, require_booking: e.target.checked })} />
                <span>…and only if a visit or meeting was booked on that call</span>
              </label>
            </div>
            <div>
              <div className="mb-1 text-xs font-semibold text-soft">Send to</div>
              <Segmented value={d.recipient} onChange={(v) => setD({ ...d, recipient: v })}
                options={[{ value: 'dialled', label: 'The number on the call' },
                          { value: 'given', label: 'The WhatsApp number they gave' }]} />
              {d.recipient === 'given' && (
                <p className="mt-1.5 text-xs text-dim">When the caller named a different WhatsApp number on the call, it goes there; otherwise to the number on the call.</p>
              )}
            </div>
          </Section>

          <Section title="What">
            <Field label="Template">
              <Select className="w-full" value={d.template_name ?? ''}
                onChange={(e) => {
                  const name = e.target.value || null
                  const t = name ? (byName.get(name) ?? [])[0] : undefined
                  setD({ ...d, template_name: name, variables: defaultVariables(t, d.variables) })
                }}>
                <option value="">Pick a template…</option>
                {[...byName.entries()].map(([name, vs]) => (
                  <option key={name} value={name}>
                    {name} — {vs.map((v) => `${langLabel(v.language)}${(v.status || '').toUpperCase() === 'APPROVED' ? '' : ` (${(v.status || '').toLowerCase()})`}`).join(', ')}
                  </option>
                ))}
              </Select>
            </Field>
            {byName.size === 0 && !tq.isLoading && (
              <Hint tone="amber">No templates yet. Create one in the Templates tab — Meta must approve it before anything can send.</Hint>
            )}
            {variants.length > 0 && (
              <div className="flex flex-wrap gap-2 text-2xs text-soft">
                {variants.map((v) => <span key={v.language} className="inline-flex items-center gap-1">{langLabel(v.language)} <TemplateStatusBadge status={v.status} /></span>)}
                <span className="text-dim">· the call’s language picks the version</span>
              </div>
            )}

            {needsMedia && (
              <div className="space-y-3 rounded-xl bg-[var(--surface-2)] p-3 ring-1 ring-[var(--border)]">
                <div>
                  <div className="text-xs font-semibold text-soft">Brochure per project ({tpl!.media})</div>
                  <p className="mt-0.5 text-xs text-dim">
                    Each caller gets the brochure of the project they agreed to. A project without a file
                    uses the default below; with neither, its messages wait until a file is attached.
                  </p>
                </div>
                {projectNames.length > 0 ? (
                  <div className="space-y-2">
                    {projectNames.map((name) => (
                      <div key={name} className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-center">
                        <span className="truncate text-sm font-medium" title={name}>{name}</span>
                        <Select className="w-full" value={d.media_map[name] ?? ''}
                          onChange={(e) => {
                            const next = { ...d.media_map }
                            if (e.target.value) next[name] = Number(e.target.value); else delete next[name]
                            setD({ ...d, media_map: next })
                          }}>
                          <option value="">Use the default file</option>
                          {(mq.data?.media ?? []).map((m) => <option key={m.id} value={m.id}>{m.filename}</option>)}
                        </Select>
                        <Button variant="ghost" size="sm" onClick={() => { setProjectUpload(name); fileRef.current?.click() }}>
                          <FileUp size={14} /> Upload
                        </Button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-dim">{d.agent_slugs.length ? 'These agents pitch no fixed project — the default file is sent.' : 'Pick agents above to list their projects.'}</p>
                )}
                <Field label="Default file">
                  <div className="flex flex-wrap items-center gap-2">
                    <Select value={d.media_id ?? ''} onChange={(e) => setD({ ...d, media_id: e.target.value ? Number(e.target.value) : null })}>
                      <option value="">None</option>
                      {(mq.data?.media ?? []).map((m) => <option key={m.id} value={m.id}>{m.filename}</option>)}
                    </Select>
                    <Button variant="ghost" size="sm" onClick={() => { setProjectUpload(null); fileRef.current?.click() }}><FileUp size={14} /> Upload</Button>
                  </div>
                </Field>
                <input ref={fileRef} type="file" hidden accept="application/pdf,image/jpeg,image/png,video/mp4"
                  onChange={async (e) => {
                    const file = e.target.files?.[0]
                    e.target.value = ''
                    if (!file) return
                    const target = projectUpload
                    try {
                      const r = await uploadWaMedia(file)
                      refresh(['wa', 'media'])
                      setD((cur) => target
                        ? { ...cur, media_map: { ...cur.media_map, [target]: r.media.id } }
                        : { ...cur, media_id: r.media.id })
                      toast(`${r.media.filename} uploaded${target ? ` for ${target}` : ''}`, 'success')
                    } catch (err: any) { toast(err?.reason || err?.message, 'error') }
                  }} />
              </div>
            )}

            <Field label="Link (optional)">
              <Input value={d.link_url ?? ''} placeholder="https://docs.google.com/forms/…"
                onChange={(e) => setD({ ...d, link_url: e.target.value || null })} />
            </Field>
            <p className="-mt-2 text-xs text-dim">
              Sent as a tracked link — so Activity shows who opened it. Placeholders fill in per call:
              add <span className="font-mono">{'{contact_name}'}</span> or{' '}
              <span className="font-mono">{'{slot:company_name}'}</span> to a Google Form’s pre-fill
              link and the caller opens a form that is already half filled. Put{' '}
              <span className="font-mono">{'{link}'}</span> in a field below, or use a template with a link button.
            </p>
          </Section>

          {textSlots.length > 0 && (
            <Section title="Fill in the template">
              <TokenChips onPick={(tok) => {
                const target = textSlots.find((s) => s.kind === 'text' && !(d.variables[s.key]?.value))
                if (target) setVar(target.key, 'value', tok)
              }} />
              <div className="space-y-3">
                {textSlots.map((s) => {
                  const ours = s.kind === 'url_suffix' && (s.base_url || '').replace(/\/$/, '').endsWith('/r')
                  return (
                    <div key={s.key} className="rounded-xl bg-[var(--surface-2)] p-3 ring-1 ring-[var(--border)]">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <span className="font-mono text-xs font-semibold">{s.label}</span>
                        {s.example && <span className="truncate text-2xs text-dim">e.g. {s.example}</span>}
                      </div>
                      {ours ? (
                        <p className="text-xs text-dim">Filled automatically with this playbook’s tracked link{d.link_url ? '' : ' — add a link above'}.</p>
                      ) : (
                        <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_160px]">
                          <Input value={d.variables[s.key]?.value ?? ''} placeholder="{contact_name}"
                            onChange={(e) => setVar(s.key, 'value', e.target.value)} />
                          <Input value={d.variables[s.key]?.fallback ?? ''} placeholder="If empty…"
                            title="Used when the value comes out empty for a call"
                            onChange={(e) => setVar(s.key, 'fallback', e.target.value)} />
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
              <p className="text-xs text-dim">
                The second box is used when a call has no value — WhatsApp refuses a message with an
                empty field. Mr./Mrs. and Sir/Ma’am appear only for contacts whose gender is on file;
                it is never guessed.
              </p>
            </Section>
          )}

          <Section title="Approval">
            <Segmented value={d.approval_mode} onChange={(v) => setD({ ...d, approval_mode: v })}
              options={[{ value: 'manual', label: 'Manual review' }, { value: 'auto', label: 'Automatic' }]} />
            <p className="text-xs text-dim">
              {d.approval_mode === 'auto'
                ? 'Sends by itself within your sending hours, minutes after the call. A message with a field it could not fill still waits for you.'
                : 'Each message waits in Approvals with the caller’s own words of agreement. You can correct names before approving.'}
            </p>
          </Section>
        </div>

        <aside className="space-y-3 lg:sticky lg:top-0 lg:self-start">
          <div className="text-xs font-semibold text-soft">Preview</div>
          <MessageBubble preview={pv?.preview ?? null}
            attachment={pv?.attachment ?? (mq.data?.media ?? []).find((m) => m.id === d.media_id)?.filename}
            empty={d.template_name ? (pvError || 'Rendering…') : 'Pick a template to see the message.'} />
          {pv && (
            <div className="space-y-2 text-xs text-dim">
              <div>
                {pv.sample
                  ? 'Shown for a sample contact — this agent has no calls yet.'
                  : <>Filled from the latest call of this agent{pv.call?.contact_name ? ` (${pv.call.contact_name})` : ''}{pv.call?.call_key && <> · <Link className="text-mint-600 hover:underline dark:text-mint-400" to={`/intelligence/${pv.call.call_key}`}>open call</Link></>}</>}
                {' '}· {langLabel(pv.language)}
                {pv.project && <> · {pv.project}</>}
              </div>
              {pv.needs_attachment && (
                <Hint tone="amber">No brochure{pv.project ? ` for ${pv.project}` : ''} yet — messages for it wait until a file is attached above.</Hint>
              )}
              {pv.missing.length > 0 && (
                <Hint tone="amber">Empty for this call: {pv.missing.join(', ')}. Add a fallback, or these messages will wait for a person to fill them in.</Hint>
              )}
              {pv.link_target && <div className="break-all">Link opens: <span className={cx('text-soft')}>{pv.link_target}</span></div>}
            </div>
          )}
        </aside>
      </div>
      </div>
    </FullscreenModal>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h3 className="eyebrow">{title}</h3>
      {children}
    </section>
  )
}
