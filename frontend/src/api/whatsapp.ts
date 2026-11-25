import { api } from './client'
import { del, patch, post, postForm, put } from './mutate'

/** Integrations → WhatsApp (dashboard/whatsapp_api.py). */
const P = '/integrations/whatsapp'

export type WaProvider = 'meta' | 'interakt'

export type WaAccount = {
  owner: string
  /** meta = the tenant's own Meta Cloud API account; interakt = an Interakt (BSP) API key. */
  provider: WaProvider
  /** Interakt only: the per-tenant webhook URL to paste into Interakt → Developer Settings. */
  webhook_url: string | null
  webhook_token: string | null
  has_webhook_secret: boolean
  waba_id: string | null
  phone_number_id: string | null
  display_phone: string | null
  verified_name: string | null
  status: 'disconnected' | 'connected' | 'error'
  quality_rating: string | null
  messaging_limit: string | null
  last_error: string | null
  connected_at: string | null
  checked_at: string | null
  default_approval: 'manual' | 'auto'
  send_window_start: number
  send_window_end: number
  send_delay_min: number
  daily_cap: number
  approval_ttl_h: number
}

export type WaPlatform = {
  send_enabled: boolean
  canary: boolean
  webhook_ready: boolean
  token_key_ready: boolean
  webhook_url: string
}

export type WaCounts = Record<string, number>

export type WaOverview = {
  account: WaAccount
  platform: WaPlatform
  counts: WaCounts
  playbooks: number
  templates: number
}

export type WaSlot = {
  key: string
  kind: 'text' | 'url_suffix' | 'document' | 'image' | 'video'
  label: string
  example?: string
  base_url?: string
}

export type WaPreview = {
  header: string | null
  header_media: string | null
  body: string
  footer: string
  buttons: { type: string; text: string; url: string | null }[]
}

export type WaTemplate = {
  id: number
  name: string
  language: string
  category: string | null
  /** meta = synced from the WABA; manual = registered here (Interakt). */
  source: 'meta' | 'manual'
  status: string | null
  parameter_format: string | null
  rejected_reason: string | null
  synced_at: string
  slots: WaSlot[]
  preview: WaPreview
  media: string
}

export type WaVariable = { value: string; fallback: string }

export type WaPlaybook = {
  id: number
  name: string
  enabled: boolean
  agent_slugs: string[]
  require_booking: boolean
  recipient: 'dialled' | 'given'
  template_name: string | null
  variables: Record<string, WaVariable>
  media_id: number | null
  media_filename?: string | null
  /** Project name → media id: the brochure of the project the caller agreed to. */
  media_map: Record<string, number>
  link_url: string | null
  approval_mode: 'manual' | 'auto'
  sends_total?: number
  sends_pending?: number
}

export type WaPlaybookDraft = Omit<WaPlaybook, 'id' | 'sends_total' | 'sends_pending' | 'media_filename'>

export type WaMedia = { id: number; filename: string; mime: string; size_bytes: number; created_at: string }

export type WaSend = {
  id: number
  playbook_id: number
  playbook_name: string | null
  approval_mode: string | null
  call_id: number | null
  call_key: string | null
  agent_slug: string | null
  contact_id: number | null
  contact_name: string | null
  to_phone: string
  template_name: string
  language: string | null
  params: Record<string, string>
  project: string | null
  media_id: number | null
  media_filename: string | null
  status: string
  reason: string | null
  not_before: string | null
  expires_at: string | null
  approved_by: string | null
  approved_at: string | null
  wamid: string | null
  error: string | null
  error_code: string | null
  pricing_category: string | null
  sent_at: string | null
  delivered_at: string | null
  read_at: string | null
  failed_at: string | null
  created_at: string
  clicks: number
  consent_quote: string | null
  consent_label: string
  preview: WaPreview | null
  slots: WaSlot[]
}

export type WaReply = {
  id: number
  phone: string
  contact_id: number | null
  contact_name: string | null
  kind: string
  body: string
  is_stop: boolean
  received_at: string
}

export type WaProject = { name: string; agents: string[] }

export type WaPlaybookPreview = {
  sample: boolean
  project: string | null
  attachment: string | null
  needs_attachment: boolean
  call: { id: number; call_key: string | null; phone: string | null; contact_name: string | null } | null
  language: string
  params: Record<string, string>
  missing: string[]
  preview: WaPreview | null
  link_target: string | null
  template_found: boolean
}

export const waOverviewPath = `${P}/overview`
export const waTemplatesPath = `${P}/templates`
export const waPlaybooksPath = `${P}/playbooks`
export const waMediaPath = `${P}/media`
export const waSendsPath = (status: string, page: number, playbookId?: number | null) =>
  `${P}/sends?status=${encodeURIComponent(status)}&page=${page}`
  + (playbookId ? `&playbook_id=${playbookId}` : '')
export const waRepliesPath = (page: number) => `${P}/replies?page=${page}`
export const waCallSendsPath = (callKey: string) => `${P}/calls/${encodeURIComponent(callKey)}`

export const waProjectsPath = (slugs: string[]) => `${P}/projects?agents=${encodeURIComponent(slugs.join(','))}`

export const connectInterakt = (body: { api_key: string; display_phone: string; webhook_secret: string }) =>
  post<{ account: WaAccount; templates: number }>(`${P}/connect/interakt`, body)
export const setWebhookSecret = (secret: string) => post<{ account: WaAccount }>(`${P}/webhook-secret`, { secret })

export type RegisteredTemplate = {
  name: string; language: string; category: 'UTILITY' | 'MARKETING' | 'AUTHENTICATION'
  header: 'none' | 'text' | 'document' | 'image' | 'video'; header_text: string
  body: string; footer: string; button_text: string; button_url: string
}
export const registerTemplate = (body: RegisteredTemplate) =>
  post<{ template: WaTemplate }>(`${P}/templates/register`, body)
export const updateRegisteredTemplate = (id: number, body: RegisteredTemplate) =>
  put<{ template: WaTemplate }>(`${P}/templates/${id}`, body)
export const deleteTemplate = (id: number) => del(`${P}/templates/${id}`)
export const deleteWaMedia = (id: number) => del(`${P}/media/${id}`)

export const connectWa = (body: { waba_id: string; phone_number_id: string; access_token: string }) =>
  post<{ account: WaAccount; templates_synced: number; template_sync_error: string | null; webhook_error: string | null }>(`${P}/connect`, body)
export const disconnectWa = () => post<{ account: WaAccount }>(`${P}/disconnect`)
export const refreshWa = () => post<{ account: WaAccount; templates_synced: number }>(`${P}/refresh`)
export const saveWaSettings = (body: Pick<WaAccount, 'default_approval' | 'send_window_start' | 'send_window_end' | 'send_delay_min' | 'daily_cap' | 'approval_ttl_h'>) =>
  patch<{ account: WaAccount }>(`${P}/settings`, body)

export const syncTemplates = () => post<{ templates_synced: number }>(`${P}/templates/sync`)
export type NewTemplate = {
  name: string; language: string; category: 'UTILITY' | 'MARKETING'; body: string; footer: string
  header: 'none' | 'document' | 'image' | 'video'; header_media_id: number | null
  button_text: string; examples: Record<string, string>
}
export const createTemplate = (body: NewTemplate) =>
  post<{ template: WaTemplate; category_changed: boolean }>(`${P}/templates`, body)

export const uploadWaMedia = (file: File) => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm<{ media: WaMedia }>(`${P}/media`, fd)
}

export const createPlaybook = (body: WaPlaybookDraft) => post<{ playbook: WaPlaybook }>(`${P}/playbooks`, body)
export const updatePlaybook = (id: number, body: WaPlaybookDraft) => put<{ playbook: WaPlaybook }>(`${P}/playbooks/${id}`, body)
export const deletePlaybook = (id: number) => del(`${P}/playbooks/${id}`)
export const previewPlaybook = (body: WaPlaybookDraft & { call_id?: number | null }) =>
  post<WaPlaybookPreview>(`${P}/playbooks/preview`, body)

export const approveSend = (id: number, params?: Record<string, string>) =>
  post<{ send: WaSend }>(`${P}/sends/${id}/approve`, params ? { params } : undefined)
export const approveSends = (ids: number[]) =>
  post<{ approved: number[]; failed: { id: number; reason: string }[] }>(`${P}/sends/approve`, { ids })
export const skipSend = (id: number) => post(`${P}/sends/${id}/skip`)
export const editSend = (id: number, params: Record<string, string>) =>
  patch<{ send: WaSend }>(`${P}/sends/${id}`, { params })

export const getWaOverview = () => api<WaOverview>(waOverviewPath)
