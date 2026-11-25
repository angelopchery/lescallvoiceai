import { api } from './client'
import { del, post } from './mutate'

/* ───────────────────────── types ───────────────────────── */
export type CopilotAction = { tool: string; summary: string; ok: boolean }

export type SessionSummary = {
  session_id: string
  title: string
  agent: string | null
  agent_name: string | null
  created_at?: string | null
  updated_at?: string | null
  message_count: number
}

export type InterviewLayer = {
  id: string
  title: string
  done: boolean
  skipped: boolean
}

export type InterviewProgress = {
  phase: 'idle' | 'interviewing' | 'built'
  complete: boolean
  layers: InterviewLayer[]
  next_layer_id: string | null
  draft: Record<string, any>
}

export type PendingChange = {
  change_id: string
  slug: string
  diff: string
  reason?: string
}

/** A playable TTS voice preview (served from /api/voices/sample/<speaker>). */
export type VoiceSample = { speaker: string; gender: string; url: string }

export type ChatResponse = {
  reply: string
  actions: CopilotAction[]
  /** Plain-English one-liner of what the turn did — what the UI shows
      instead of per-tool chips. */
  actions_summary?: string
  /** Agents touched this turn that exist on disk — the only slugs the UI
      may offer Try/Open/Activate for (a mid-interview draft is not tryable). */
  agent_links?: string[]
  agents_changed: boolean
  session?: SessionSummary
  interview?: InterviewProgress
  pending_changes?: PendingChange[]
  voice_samples?: VoiceSample[]
}

export type TranscriptMsg = {
  role: 'user' | 'assistant'
  content: string
  actions?: CopilotAction[]
  actions_summary?: string
  agent_links?: string[]
  voice_samples?: VoiceSample[]
}

/* ───────────────────────── calls ───────────────────────── */
export const copilotChat = (session_id: string, message: string, attachments?: string[]) =>
  api<ChatResponse>('/copilot/chat', {
    method: 'POST',
    body: JSON.stringify(
      attachments?.length ? { session_id, message, attachments } : { session_id, message },
    ),
  })

export const copilotSession = (session_id: string) =>
  api<{
    session_id: string
    transcript: TranscriptMsg[]
    session?: SessionSummary
    interview?: InterviewProgress
    pending_changes?: PendingChange[]
  }>(`/copilot/sessions/${session_id}`)

/* ───────────────── quick controls: voice + speed by click ───────────────── */
export type RosterVoice = {
  speaker: string
  gender: 'male' | 'female' | ''
  /** Pre-generated stock sample; null → preview is synthesized live. */
  sample_url: string | null
  recommended_for: string[]
}

export type QuickState = {
  languages: { code: string; name: string }[]
  default_language: string
  speaker: string | null
  mixed_speakers: boolean
  gender: 'male' | 'female' | ''
  pronouns: string
  model: string
  pace: Record<string, number>
}

export type QuickTarget = {
  kind: 'agent' | 'draft' | 'none'
  slug: string | null
  twin: string | null
  display_name: string
}

export type QuickControls = {
  target: QuickTarget
  state: QuickState | Record<string, never>
  roster: RosterVoice[]
  pace_range: { min: number; max: number; step: number; default: number }
}

export type QuickApplyResult = {
  target: QuickTarget
  state: QuickState
  revision?: number
  twin_write?: { applied: boolean; slug?: string; reason?: string }
  interview?: InterviewProgress
  pending_changes?: PendingChange[]
}

export const copilotQuickControls = (session_id: string) =>
  api<QuickControls>(`/copilot/sessions/${session_id}/quick-controls`)

export const applyQuickControls = (
  session_id: string,
  body: { speaker?: string; pace?: number | Record<string, number>; both_directions?: boolean },
) => post<QuickApplyResult>(`/copilot/sessions/${session_id}/quick-controls`, body)

export const copilotSessions = () =>
  api<{ sessions: SessionSummary[] }>('/copilot/sessions')

export const copilotDeleteSession = (session_id: string) =>
  del(`/copilot/sessions/${session_id}`)

/** Attach a document to the conversation; pass the doc_id in the next chat. */
export async function copilotUpload(session_id: string, file: File) {
  const fd = new FormData()
  fd.append('session_id', session_id)
  fd.append('file', file)
  const res = await fetch('/api/copilot/upload', { method: 'POST', body: fd })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<{ doc_id: string; session_id: string }>
}

/** Diff-card buttons — the click is the confirmation. */
export const applyPendingChange = (session_id: string, change_id: string) =>
  post<{ status: string; revision: number }>(
    `/copilot/sessions/${session_id}/changes/${change_id}/apply`)

export const discardPendingChange = (session_id: string, change_id: string) =>
  post<{ status: string }>(
    `/copilot/sessions/${session_id}/changes/${change_id}/discard`)
