/**
 * Intelligence browsing state that has to OUTLIVE the list page.
 *
 * Opening a call unmounts the list, and everything the list held in component
 * state went with it: the AI filter, its verified rows, the ticked selection.
 * Coming back meant typing the filter again and paying for another judged
 * pass, once per call inspected. The list also had nowhere to hand its row
 * order to, so the call page could not offer "next".
 *
 * What lives where, on purpose:
 *  - WHICH filter is active is in the URL (`?ai=`), so Back, refresh and a
 *    shared link all land on the same view.
 *  - The RESULT of that filter is here. A verified pass reads up to 300 calls
 *    with a model; re-deriving it on every Back is the cost this removes.
 *
 * Memory is the fast tier (SPA navigation); sessionStorage is the tier that
 * survives a refresh. Every storage access is guarded — private windows and
 * blocked site data throw — and the page works with both tiers empty: it just
 * runs the filter named in the URL again.
 */
import type { IntelCallRow, Verification } from '../api/intel'

/** One completed AI-filter pass, exactly as the server answered it. */
export type AiFilterSnapshot = {
  /** The phrase as typed — the value of `?ai=`. */
  query: string
  /** The agent dropdown (CSV) the pass ran under; part of the identity. */
  agent: string
  rows: IntelCallRow[]
  applied: { agent: string; query: string } | null
  verification: Verification | null
  verified: boolean
  degraded: boolean
  /** Epoch ms the pass finished. A restored result says how old it is. */
  at: number
}

/**
 * The ordered run of calls the user is walking through — what Previous/Next
 * on the call page steps along.
 */
export type CallSequence = {
  /** call_keys in the order the list showed them. */
  keys: string[]
  /** Index of keys[0] within the WHOLE result. An AI filter arrives complete
   *  (0); the structured list is server-paged, so a sequence begins as one
   *  page of it and grows at either end as the user walks past the edge. */
  offset: number
  /** Size of the whole result, not of `keys`. */
  total: number
  pageSize: number
  /** The list URL's query string WITHOUT `page` — where "back" returns to.
   *  The page is derived from the call being viewed, so Back lands on the
   *  page that contains it rather than the page the user left from. */
  listSearch: string
  /** Server predicates (no page/limit) for fetching the neighbouring page of
   *  a structured list. `null` = the sequence is already the whole result. */
  filterQs: string | null
  /** Short human name of the list, for the pager's tooltip. */
  label: string
  /** The call most recently opened from this sequence. */
  lastViewed: string | null
  /** Set while a call from this sequence is open; consumed by the list the
   *  next time it mounts. "The user is coming back from a call" has to be a
   *  recorded fact: the router reports POP for Back AND for a plain refresh,
   *  so inferring it from the navigation type moved a refreshed list to
   *  another page. */
  pendingReturn?: boolean
}

/** Ticked rows. They belong to the filter that produced them (`key`). */
export type SelectionSnapshot = { key: string; ids: number[]; capped: boolean }

const K_AI = 'namaskall.intel.ai.v1'
const K_SEQ = 'namaskall.intel.seq.v1'
const K_SEL = 'namaskall.intel.sel.v1'

const mem = new Map<string, unknown>()

function read<T>(k: string): T | null {
  if (mem.has(k)) return mem.get(k) as T
  try {
    const raw = window.sessionStorage.getItem(k)
    if (!raw) return null
    const val = JSON.parse(raw) as T
    mem.set(k, val)
    return val
  } catch {
    return null
  }
}

function write(k: string, val: unknown) {
  if (val == null) mem.delete(k)
  else mem.set(k, val)
  try {
    if (val == null) window.sessionStorage.removeItem(k)
    else window.sessionStorage.setItem(k, JSON.stringify(val))
  } catch {
    /* Quota or blocked storage: the memory tier still carries this session. */
  }
}

/* ------------------------------------------------------------ AI filter */

/** The stored pass, only if it is THE pass for this query + agent scope. */
export function loadAiFilter(query: string, agent: string): AiFilterSnapshot | null {
  if (!query) return null
  const s = read<AiFilterSnapshot>(K_AI)
  return s && s.query === query && s.agent === agent && Array.isArray(s.rows) ? s : null
}

/** Whatever pass is stored, whether or not the URL still names it — what the
 *  list offers to resume after the user left through the sidebar. */
export function loadLastAiFilter(): AiFilterSnapshot | null {
  const s = read<AiFilterSnapshot>(K_AI)
  return s && s.query && Array.isArray(s.rows) ? s : null
}

export function saveAiFilter(s: AiFilterSnapshot | null) { write(K_AI, s) }

/* ------------------------------------------------------------- sequence */

export function loadSequence(): CallSequence | null {
  const s = read<CallSequence>(K_SEQ)
  return s && Array.isArray(s.keys) ? s : null
}

export function saveSequence(s: CallSequence | null) { write(K_SEQ, s) }

/** Record which call of the sequence is open. No-op for a call that is not in
 *  it (a deep link, a call opened from another page). */
export function markViewed(callKey: string) {
  const s = loadSequence()
  if (!s || !s.keys.includes(callKey)) return
  if (s.lastViewed !== callKey || !s.pendingReturn) {
    saveSequence({ ...s, lastViewed: callKey, pendingReturn: true })
  }
}

/** The list has handled the return (page + scroll); a later refresh of it is
 *  just a refresh. The "last opened" marker itself stays. */
export function consumeReturn() {
  const s = loadSequence()
  if (s?.pendingReturn) saveSequence({ ...s, pendingReturn: false })
}

/** Where `callKey` sits in the whole result (0-based), or -1. */
export function positionOf(s: CallSequence | null, callKey: string): number {
  if (!s) return -1
  const i = s.keys.indexOf(callKey)
  return i < 0 ? -1 : s.offset + i
}

/** The list page (1-based) that contains whole-result position `pos`. */
export function pageOf(s: CallSequence, pos: number): number {
  return Math.floor(pos / Math.max(1, s.pageSize)) + 1
}

/** The list URL that shows `callKey` — filter intact, on the right page. */
export function listUrlFor(s: CallSequence | null, callKey: string): string {
  if (!s) return '/intelligence'
  const p = new URLSearchParams(s.listSearch)
  p.delete('page')
  const pos = positionOf(s, callKey)
  const page = pos >= 0 ? pageOf(s, pos) : 1
  if (page > 1) p.set('page', String(page))
  const qs = p.toString()
  return qs ? `/intelligence?${qs}` : '/intelligence'
}

/**
 * Grow a paged sequence with a neighbouring page's keys.
 *
 * Offset paging over a list that is still receiving calls SHIFTS: one new
 * call pushes every row down by one, so the next page's first key is the
 * current page's last. Keys already present are dropped, which turns that
 * shift into a harmless no-op instead of showing the same call twice.
 */
export function extendSequence(
  s: CallSequence, side: 'before' | 'after', keys: string[], total?: number,
): CallSequence {
  const have = new Set(s.keys)
  const fresh = keys.filter((k) => k && !have.has(k))
  const next: CallSequence = { ...s, total: total ?? s.total }
  if (side === 'after') next.keys = [...s.keys, ...fresh]
  else {
    next.keys = [...fresh, ...s.keys]
    next.offset = Math.max(0, s.offset - fresh.length)
  }
  return next
}

/* ------------------------------------------------------------ selection */

export function loadSelection(key: string): SelectionSnapshot | null {
  const s = read<SelectionSnapshot>(K_SEL)
  return s && s.key === key && Array.isArray(s.ids) ? s : null
}

export function saveSelection(s: SelectionSnapshot | null) {
  write(K_SEL, s && s.ids.length ? s : null)
}
