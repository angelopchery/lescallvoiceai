/**
 * Agent share links.
 *
 * Sharing hands someone a COPY, not access: the recipient's import creates an
 * independent agent in their own account with its own slug, and neither side's
 * edits ever reach the other. Nothing here creates a live relationship, which
 * is why there is no "shared with me" list to keep in sync — once imported, it
 * is just one of your agents.
 *
 * The same link can be opened again, because that is how it is really used:
 * someone builds an agent for you, improves it, and re-sends it. The second
 * open is a choice — REPLACE a copy you hold, or take a NEW one. Replace is
 * still a one-off copy, not a subscription.
 */
import { api } from './client'
import { post, del } from './mutate'

/** A link the signed-in user created for one of their own agents. */
export type ShareLink = {
  token: string
  /** Full https URL to hand out — short `/s/<token>` form. */
  url: string
  family: string
  label: string
  state: 'live' | 'revoked' | 'expired'
  import_count: number
  expires_at: string | null
  created_at: string
}

/** What the import popup renders about someone else's agent. */
export type SharePreview = {
  token: string
  agent: {
    agent_name: string
    company: string
    languages: string[]
    stages: string[]
    directions: ('inbound' | 'outbound')[]
    variants: { slug: string; direction: 'inbound' | 'outbound' }[]
    calendar_slots: number
    documents: number
  }
  shared_by: { email: string; name: string }
  expires_at: string | null
  /** When the sharer last changed the agent. */
  source_updated_at: string | null
  /** May this account take a NEW copy? Holding a copy already does not make
      this false — being at the plan cap or over the daily ceiling does. */
  can_import: boolean
  /** Human sentence explaining why a NEW copy is unavailable. */
  reason: string
  /** 'cap_agents' | 'rate_limited' | '' */
  code: string
  billing: { agents_used?: number; agents_limit?: number | null; enforcement?: boolean }
  /** Live agents in MY account that came from this link. Non-empty means the
      dialog offers Replace — which needs no plan slot, so it stays available
      to an account at its cap. */
  copies: ShareCopy[]
}

/** One earlier import of a link that is still live in my account. */
export type ShareCopy = {
  family: string
  /** Its live slugs only — a direction I archived is not listed. */
  slugs: string[]
  primary: string
  imported_at: string
  /** Last time it was replaced from the link; null = never. */
  replaced_at: string | null
}

export type ImportResult = {
  status: 'imported' | 'replaced'
  family: string
  slugs: string[]
  /** The slug to open in the editor. */
  primary: string
  /** replace only ↓ */
  replaced?: string[]
  /** Directions created from the shared version because my copy lacked them. */
  added?: string[]
  skipped?: string[]
  documents?: { updated: number; removed: number; kept: number; failed?: boolean }
}

export type ImportOptions =
  | { mode: 'new' }
  | { mode: 'replace'; target: string }

/* ── sharer ── */
export const getShareLink = (slug: string) =>
  api<{ link: ShareLink | null; family: string }>(`/share?slug=${encodeURIComponent(slug)}`)

/** Idempotent: calling twice returns the SAME link rather than a second one. */
export const createShareLink = (slug: string) =>
  post<{ link: ShareLink; created: boolean }>('/share', { slug })

export const revokeShareLink = (token: string) =>
  del<{ status: string; import_count: number }>(`/share/${token}`)

/* ── recipient ── */
export const previewShare = (token: string) =>
  api<SharePreview>(`/share/${encodeURIComponent(token)}`)

/** No options = the first import. Once a copy exists the server refuses a
    bare call (409) rather than guess between the two. */
export const importShare = (token: string, opts?: ImportOptions) =>
  post<ImportResult>(`/share/${encodeURIComponent(token)}/import`, opts)
