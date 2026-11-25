import { useMemo, useRef, useState } from 'react'
import { FileUp, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { Badge, Button, Card, IconButton } from '../../../components/ui'
import { AsyncState, Field, Input, Segmented, Select, Textarea } from '../../../components/kit'
import { Modal } from '../../../components/Modal'
import { useToast } from '../../../components/Toast'
import { useConfirm } from '../../../components/confirm'
import { useGet, useRefresh } from '../../../api/hooks/useApi'
import {
  createTemplate, deleteTemplate, registerTemplate, syncTemplates, updateRegisteredTemplate,
  uploadWaMedia, waMediaPath, waTemplatesPath,
  type NewTemplate, type RegisteredTemplate, type WaMedia, type WaOverview, type WaTemplate,
} from '../../../api/whatsapp'
import { isConnected } from './SetupTab'
import { Hint, MessageBubble, TemplateStatusBadge, TokenChips, langLabel } from './shared'

export function TemplatesTab({ data }: { data: WaOverview }) {
  const q = useGet<{ templates: WaTemplate[] }>(['wa', 'templates'], waTemplatesPath)
  const toast = useToast()
  const confirm = useConfirm()
  const refresh = useRefresh()
  const [creating, setCreating] = useState(false)
  const [registering, setRegistering] = useState<{ id: number | null; initial: RegisteredTemplate } | null>(null)
  const [syncing, setSyncing] = useState(false)
  const connected = isConnected(data.account)
  const interakt = data.account.provider === 'interakt'

  // One card per template NAME: its language variants are one message to the
  // author, and a playbook picks a name and lets the call pick the language.
  const groups = useMemo(() => {
    const m = new Map<string, WaTemplate[]>()
    for (const t of q.data?.templates ?? []) m.set(t.name, [...(m.get(t.name) ?? []), t])
    return [...m.entries()]
  }, [q.data])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-sm text-dim">
          {interakt
            ? <>WhatsApp only lets a business start a conversation with a template Meta has approved. Create and approve it in <span className="font-semibold">Interakt → Templates</span>, then add it here with the same name and text so each call can fill in its own names and the right brochure.</>
            : <>WhatsApp only lets a business start a conversation with a template Meta has approved. Write it once with placeholders; each call fills in its own names and details.</>}
        </p>
        <div className="flex gap-2">
          {!interakt && (
            <Button variant="ghost" size="sm" disabled={!connected || syncing}
              onClick={async () => {
                setSyncing(true)
                try {
                  const r = await syncTemplates()
                  toast(`${r.templates_synced} template${r.templates_synced === 1 ? '' : 's'} synced from Meta`, 'success')
                  refresh(['wa'])
                } catch (e: any) { toast(e?.reason || e?.message, 'error') } finally { setSyncing(false) }
              }}>
              <RefreshCw size={14} /> Sync from Meta
            </Button>
          )}
          {interakt ? (
            <Button size="sm" disabled={!connected} onClick={() => setRegistering({ id: null, initial: STARTER })}>
              <Plus size={14} /> Add approved template
            </Button>
          ) : (
            <Button size="sm" disabled={!connected} onClick={() => setCreating(true)}>
              <Plus size={14} /> New template
            </Button>
          )}
        </div>
      </div>
      {!connected && (
        <Hint tone="amber">Connect your WhatsApp number in Setup to add templates.</Hint>
      )}
      {interakt && connected && groups.length === 0 && !q.isLoading && (
        <Hint tone="mint">
          Start from the brochure message: press <span className="font-semibold">Add approved template</span>, copy
          its text into a new Utility template in Interakt (same name), and once Interakt shows it approved, save it here.
        </Hint>
      )}
      <AsyncState loading={q.isLoading} error={q.error} variant="cards" rows={3}
        empty={groups.length === 0}
        emptyLabel={connected
          ? (interakt
            ? 'No templates added yet. Add the approved template from Interakt here — the playbooks pick from this list.'
            : 'No templates on this WhatsApp account yet. Create one here, or in Meta’s WhatsApp Manager and press Sync.')
          : 'Templates appear here once a number is connected.'}>
        <div className="grid gap-4 lg:grid-cols-2">
          {groups.map(([name, variants]) => (
            <Card key={name} className="p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-mono text-sm font-semibold">{name}</div>
                  <div className="text-2xs uppercase tracking-wide text-dim">{variants[0].category || '—'}</div>
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {variants.map((v) => (
                    <span key={v.language} className="inline-flex items-center gap-1 text-2xs text-soft">
                      {langLabel(v.language)}{' '}
                      {v.source === 'manual' ? <Badge tone="mint">Added from Interakt</Badge> : <TemplateStatusBadge status={v.status} />}
                    </span>
                  ))}
                  {variants[0].source === 'manual' && (
                    <>
                      <IconButton label={`Edit ${name}`} onClick={() => setRegistering({ id: variants[0].id, initial: fromTemplate(variants[0]) })}>
                        <Pencil size={14} />
                      </IconButton>
                      <IconButton label={`Remove ${name}`} onClick={async () => {
                        if (await confirm({
                          title: `Remove “${name}”?`,
                          description: 'Only our copy is removed — the template stays in Interakt. A playbook that still uses it must pick another first.',
                          destructive: true,
                        })) {
                          try { await deleteTemplate(variants[0].id); refresh(['wa']) } catch (e: any) { toast(e?.reason || e?.message, 'error') }
                        }
                      }}><Trash2 size={14} /></IconButton>
                    </>
                  )}
                </div>
              </div>
              <MessageBubble preview={variants[0].preview} />
              {variants.filter((v) => v.rejected_reason).map((v) => (
                <Hint key={v.language} tone="rose" className="mt-2">
                  {langLabel(v.language)} rejected: {v.rejected_reason!.replace(/_/g, ' ').toLowerCase()}
                </Hint>
              ))}
            </Card>
          ))}
        </div>
      </AsyncState>
      <NewTemplateModal open={creating} onClose={() => setCreating(false)} />
      {registering && (
        <RegisterTemplateModal id={registering.id} initial={registering.initial} onClose={() => setRegistering(null)} />
      )}
    </div>
  )
}

/* ------------------------------------------------------------ Interakt: register */

/** The brochure message Pramukh-style agents promise on the call. Variables are
 *  positional, as Interakt writes them: 1 = greeting, 2 = agent, 3 = project. */
const STARTER: RegisteredTemplate = {
  name: 'pramukh_brochure', language: 'en', category: 'UTILITY', header: 'document', header_text: '',
  body: 'Hi {{1}}, thank you for speaking with {{2}} from Pramukh Group today. As promised, here is the {{3}} brochure. Reply here or call us to plan your site visit.',
  footer: 'Reply STOP to stop these messages', button_text: '', button_url: '',
}

function fromTemplate(t: WaTemplate): RegisteredTemplate {
  const comps = (t as any).components as any[] | undefined
  const find = (type: string) => (comps ?? []).find((c) => (c.type || '').toUpperCase() === type)
  const header = find('HEADER')
  const button = (find('BUTTONS')?.buttons ?? []).find((b: any) => (b.type || '').toUpperCase() === 'URL')
  const fmt = (header?.format || '').toLowerCase()
  return {
    name: t.name, language: t.language,
    category: ((t.category || 'UTILITY').toUpperCase() as RegisteredTemplate['category']),
    header: (['document', 'image', 'video', 'text'].includes(fmt) ? fmt : 'none') as RegisteredTemplate['header'],
    header_text: fmt === 'text' ? header?.text || '' : '',
    body: find('BODY')?.text || t.preview.body || '',
    footer: find('FOOTER')?.text || '',
    button_text: button?.text || '', button_url: button?.url || '',
  }
}

function RegisterTemplateModal({ id, initial, onClose }: { id: number | null; initial: RegisteredTemplate; onClose: () => void }) {
  const toast = useToast()
  const refresh = useRefresh()
  const [f, setF] = useState<RegisteredTemplate>(initial)
  const [busy, setBusy] = useState(false)
  const [caret, setCaret] = useState<number | null>(null)
  const vars = useMemo(() => {
    const out: string[] = []
    for (const m of f.body.matchAll(/\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g)) if (!out.includes(m[1])) out.push(m[1])
    return out
  }, [f.body])
  const nextIndex = useMemo(() => {
    const nums = vars.map((v) => Number(v)).filter((n) => Number.isFinite(n))
    return (nums.length ? Math.max(...nums) : 0) + 1
  }, [vars])
  const problems = useMemo(() => {
    const p: string[] = []
    if (f.name && !/^[a-z0-9_]+$/.test(f.name)) p.push('Name: lowercase letters, digits and underscores only — copy the code name from Interakt.')
    if (f.button_text && f.button_url && !/^https?:\/\/\S+$/.test(f.button_url)) p.push('The button link must start with https://')
    return p
  }, [f.name, f.button_text, f.button_url])

  function insert(token: string) {
    const at = caret ?? f.body.length
    setF({ ...f, body: f.body.slice(0, at) + token + f.body.slice(at) })
    setCaret(at + token.length)
  }

  const preview = {
    header: f.header === 'text' ? f.header_text || null : null,
    header_media: f.header === 'none' || f.header === 'text' ? null : f.header,
    body: f.body, footer: f.footer,
    buttons: f.button_text ? [{ type: 'URL', text: f.button_text, url: f.button_url || null }] : [],
  }

  return (
    <Modal open onClose={onClose} wide title={id == null ? 'Add a template approved in Interakt' : `Edit “${initial.name}”`}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button disabled={busy || !f.name || !f.body.trim() || problems.length > 0}
          onClick={async () => {
            setBusy(true)
            try {
              if (id == null) await registerTemplate(f)
              else await updateRegisteredTemplate(id, f)
              toast(id == null ? 'Template added — pick it in a playbook' : 'Template updated', 'success')
              refresh(['wa'])
              onClose()
            } catch (e: any) { toast(e?.reason || e?.message, 'error') } finally { setBusy(false) }
          }}>{id == null ? 'Add template' : 'Save changes'}</Button>
      </>}>
      <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_260px]">
        <div className="space-y-3">
          <Hint tone="neutral">
            Write it exactly as it is in Interakt — same code name (the one in the template’s URL), same language,
            same text with <span className="font-mono">{'{{1}}'}</span>, <span className="font-mono">{'{{2}}'}</span>… where Interakt has variables.
            Interakt only sends what Meta approved, so a difference here would show in the preview, not on the phone.
          </Hint>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Template code name">
              <Input value={f.name} placeholder="pramukh_brochure"
                onChange={(e) => setF({ ...f, name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_') })} />
            </Field>
            <Field label="Language">
              <Select className="w-full" value={f.language} onChange={(e) => setF({ ...f, language: e.target.value })}>
                {['en', 'hi', 'gu', 'en_US', 'mr'].map((l) => <option key={l} value={l}>{langLabel(l)}</option>)}
              </Select>
            </Field>
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-soft">Category (as in Interakt)</div>
            <Segmented value={f.category} onChange={(v) => setF({ ...f, category: v })}
              options={[{ value: 'UTILITY', label: 'Utility' }, { value: 'MARKETING', label: 'Marketing' }]} />
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-soft">Header</div>
            <Segmented value={f.header} onChange={(v) => setF({ ...f, header: v })}
              options={[{ value: 'none', label: 'None' }, { value: 'document', label: 'PDF' },
                        { value: 'image', label: 'Image' }, { value: 'video', label: 'Video' }, { value: 'text', label: 'Text' }]} />
            {f.header === 'text' ? (
              <Input className="mt-2" value={f.header_text} maxLength={60} placeholder="Header text"
                onChange={(e) => setF({ ...f, header_text: e.target.value })} />
            ) : f.header !== 'none' && (
              <p className="mt-1.5 text-xs text-dim">The brochure (or image) goes here. Each playbook attaches its own file per project.</p>
            )}
          </div>
          <Field label="Message">
            <Textarea rows={5} value={f.body} maxLength={1024}
              onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
              onChange={(e) => { setF({ ...f, body: e.target.value }); setCaret(e.target.selectionStart) }} />
          </Field>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => insert(`{{${nextIndex}}}`)}>
              <Plus size={13} /> Insert variable {'{{'}{nextIndex}{'}}'}
            </Button>
            <span className="text-xs text-dim">{vars.length ? `${vars.length} variable${vars.length === 1 ? '' : 's'} — the playbook says what fills each one` : 'No variables yet'}</span>
          </div>
          <TokenChips onPick={insert} tokens={[
            { token: '{greeting}', hint: 'Use our placeholders instead of {{n}} if you prefer — they become named variables' },
            { token: '{project_name}', hint: 'The project the caller agreed to receive' },
            { token: '{agent_name}', hint: 'The agent who spoke to them' },
          ]} />
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Footer (optional)">
              <Input value={f.footer} maxLength={60} onChange={(e) => setF({ ...f, footer: e.target.value })} />
            </Field>
            <Field label="Button text (optional)">
              <Input value={f.button_text} maxLength={25} placeholder="e.g. Call us"
                onChange={(e) => setF({ ...f, button_text: e.target.value })} />
            </Field>
            <Field label="Button link">
              <Input value={f.button_url} placeholder="https://…" disabled={!f.button_text}
                onChange={(e) => setF({ ...f, button_url: e.target.value })} />
            </Field>
          </div>
          {problems.map((p) => <Hint key={p} tone="rose">{p}</Hint>)}
        </div>
        <div>
          <div className="mb-2 text-xs font-semibold text-soft">Preview</div>
          <MessageBubble preview={preview} />
          <div className="mt-3 flex flex-wrap gap-1">
            <Badge tone="neutral">{langLabel(f.language)}</Badge>
            <Badge tone={f.category === 'UTILITY' ? 'mint' : 'amber'}>{f.category.toLowerCase()}</Badge>
          </div>
        </div>
      </div>
    </Modal>
  )
}

/* ------------------------------------------------------------ Meta: create */

const EMPTY: NewTemplate = {
  name: '', language: 'en', category: 'UTILITY',
  body: 'Hi {greeting}, thank you for speaking with us today. As discussed, here are the details of {project_name}.',
  footer: 'Reply STOP to stop these messages', header: 'none', header_media_id: null,
  button_text: '', examples: {},
}

function NewTemplateModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast()
  const refresh = useRefresh()
  const media = useGet<{ media: WaMedia[] }>(['wa', 'media'], waMediaPath, { enabled: open })
  const [f, setF] = useState<NewTemplate>(EMPTY)
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  // Where the caret last was, so a placeholder chip inserts there rather than
  // at the end. Tracked from events: the kit Textarea does not forward refs.
  const [caret, setCaret] = useState<number | null>(null)

  const names = useMemo(() => {
    const out: string[] = []
    for (const m of f.body.matchAll(/\{\s*([a-z][a-z0-9_]{0,30})\s*\}/g)) if (!out.includes(m[1])) out.push(m[1])
    return out
  }, [f.body])
  const problems = useMemo(() => {
    const p: string[] = []
    if (f.name && !/^[a-z0-9_]+$/.test(f.name)) p.push('Name: lowercase letters, digits and underscores only.')
    if (/^\s*\{/.test(f.body)) p.push('Meta rejects a message that starts with a placeholder.')
    if (/\}\s*[.!?]?\s*$/.test(f.body)) p.push('Meta rejects a message that ends with a placeholder.')
    if (/\}\s*\{/.test(f.body)) p.push('Put a word between two placeholders.')
    return p
  }, [f.name, f.body])

  function insert(token: string) {
    const at = caret ?? f.body.length
    setF({ ...f, body: f.body.slice(0, at) + token + f.body.slice(at) })
    setCaret(at + token.length)
  }

  const preview = {
    header: null, header_media: f.header === 'none' ? null : f.header,
    body: f.body.replace(/\{\s*([a-z][a-z0-9_]{0,30})\s*\}/g, (_, n) => f.examples[n] || `{{${n}}}`),
    footer: f.footer,
    buttons: f.button_text ? [{ type: 'URL', text: f.button_text, url: null }] : [],
  }

  return (
    <Modal open={open} onClose={onClose} wide title="New message template"
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button disabled={busy || !f.name || !f.body.trim() || problems.length > 0
          || (f.header !== 'none' && !f.header_media_id)}
          onClick={async () => {
            setBusy(true)
            try {
              const r = await createTemplate(f)
              toast(r.category_changed
                ? `Sent for review — note Meta classed it as ${r.template.category}, which is priced higher`
                : 'Sent to Meta for review. Approval usually takes minutes, sometimes a day.', r.category_changed ? 'warn' : 'success')
              refresh(['wa'])
              setF(EMPTY)
              onClose()
            } catch (e: any) { toast(e?.reason || e?.message, 'error') } finally { setBusy(false) }
          }}>Submit for review</Button>
      </>}>
      <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_260px]">
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Template name">
              <Input value={f.name} placeholder="swarit_brochure"
                onChange={(e) => setF({ ...f, name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_') })} />
            </Field>
            <Field label="Language">
              <Select className="w-full" value={f.language} onChange={(e) => setF({ ...f, language: e.target.value })}>
                {['en', 'hi', 'gu', 'en_US'].map((l) => <option key={l} value={l}>{langLabel(l)}</option>)}
              </Select>
            </Field>
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-soft">Category</div>
            <Segmented value={f.category} onChange={(v) => setF({ ...f, category: v })}
              options={[{ value: 'UTILITY', label: 'Utility' }, { value: 'MARKETING', label: 'Marketing' }]} />
            <p className="mt-1.5 text-xs text-dim">
              Utility is for what the caller asked for — details, a brochure, a visit confirmation —
              and costs a fraction of Marketing. Promotional wording (“offer”, “limited time”)
              gets it re-classed as Marketing by Meta.
            </p>
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-soft">Attachment at the top</div>
            <Segmented value={f.header} onChange={(v) => setF({ ...f, header: v })}
              options={[{ value: 'none', label: 'None' }, { value: 'document', label: 'PDF' },
                        { value: 'image', label: 'Image' }]} />
            {f.header !== 'none' && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Select value={f.header_media_id ?? ''} onChange={(e) => setF({ ...f, header_media_id: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">Example file for Meta’s review…</option>
                  {(media.data?.media ?? []).filter((m) => (f.header === 'document') === (m.mime === 'application/pdf'))
                    .map((m) => <option key={m.id} value={m.id}>{m.filename}</option>)}
                </Select>
                <Button variant="ghost" size="sm" onClick={() => fileRef.current?.click()}><FileUp size={14} /> Upload</Button>
                <input ref={fileRef} type="file" hidden accept={f.header === 'document' ? 'application/pdf' : 'image/jpeg,image/png'}
                  onChange={async (e) => {
                    const file = e.target.files?.[0]
                    e.target.value = ''
                    if (!file) return
                    try {
                      const r = await uploadWaMedia(file)
                      refresh(['wa', 'media'])
                      setF((cur) => ({ ...cur, header_media_id: r.media.id }))
                    } catch (err: any) { toast(err?.reason || err?.message, 'error') }
                  }} />
                <p className="w-full text-xs text-dim">Each playbook attaches its own file when it sends — this one is only the sample Meta reviews.</p>
              </div>
            )}
          </div>
          <Field label="Message">
            <Textarea rows={5} value={f.body} maxLength={1024}
              onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
              onChange={(e) => { setF({ ...f, body: e.target.value }); setCaret(e.target.selectionStart) }} />
          </Field>
          <TokenChips onPick={insert} />
          {names.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs font-semibold text-soft">Example values Meta reviews</div>
              {names.map((n) => (
                <div key={n} className="flex items-center gap-2">
                  <span className="w-32 shrink-0 truncate font-mono text-2xs text-dim">{`{${n}}`}</span>
                  <Input value={f.examples[n] ?? ''} placeholder={n.replace(/_/g, ' ')}
                    onChange={(e) => setF({ ...f, examples: { ...f.examples, [n]: e.target.value } })} />
                </div>
              ))}
            </div>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Footer (optional)">
              <Input value={f.footer} maxLength={60} onChange={(e) => setF({ ...f, footer: e.target.value })} />
            </Field>
            <Field label="Link button (optional)">
              <Input value={f.button_text} maxLength={25} placeholder="e.g. Open the form"
                onChange={(e) => setF({ ...f, button_text: e.target.value })} />
            </Field>
          </div>
          {f.button_text && (
            <p className="text-xs text-dim">The button opens the playbook’s link through a tracked address, so you see who clicked.</p>
          )}
          {problems.map((p) => <Hint key={p} tone="rose">{p}</Hint>)}
        </div>
        <div>
          <div className="mb-2 text-xs font-semibold text-soft">Preview</div>
          <MessageBubble preview={preview} />
          <div className="mt-3 flex flex-wrap gap-1">
            <Badge tone="neutral">{langLabel(f.language)}</Badge>
            <Badge tone={f.category === 'UTILITY' ? 'mint' : 'amber'}>{f.category.toLowerCase()}</Badge>
          </div>
        </div>
      </div>
    </Modal>
  )
}
