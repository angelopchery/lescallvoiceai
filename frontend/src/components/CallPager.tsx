/**
 * Previous / Next through the list a call was opened from.
 *
 * Reviewing a filtered list used to be: open a call, go back, find your place,
 * open the next. The list now hands its row order to lib/intelBrowse when a
 * row is opened, and this walks it — so a 44-call answer is 43 presses of one
 * button (or the → key) instead of 43 round trips.
 *
 * Steps REPLACE the history entry rather than pushing one. The user is sliding
 * along a list, not drilling deeper: Back must return to the list in one press
 * no matter how many calls were stepped through, not replay each of them.
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { api } from '../api/client'
import type { IntelCallRow, IntelDetail } from '../api/intel'
import {
  extendSequence, listUrlFor, loadSequence, markViewed, saveSequence,
  type CallSequence,
} from '../lib/intelBrowse'
import { IconButton, Spinner } from './ui'
import { useToast } from './Toast'

/** Nearest openable key in a direction. '' marks a row that had no call_key;
 *  it holds its position (so pages line up with the list) but is stepped over. */
function neighbour(s: CallSequence, from: number, dir: 1 | -1): string | null {
  for (let i = from + dir; i >= 0 && i < s.keys.length; i += dir) {
    if (s.keys[i]) return s.keys[i]
  }
  return null
}

export function useCallSequence(callKey: string) {
  const nav = useNavigate()
  const qc = useQueryClient()
  const toast = useToast()
  const [seq, setSeq] = useState<CallSequence | null>(() => loadSequence())
  const [busy, setBusy] = useState(false)

  const index = seq ? seq.keys.indexOf(callKey) : -1
  // A call reached some other way (a pasted link, the contact hub) is in no
  // sequence. It gets no pager rather than one that walks an unrelated list.
  const active = Boolean(seq) && index >= 0

  useEffect(() => { if (active) markViewed(callKey) }, [active, callKey])

  // A server-paged list holds one page of keys; more exist past either end.
  const paged = active && seq!.filterQs != null
  const hasPrev = active && (neighbour(seq!, index, -1) != null || (paged && seq!.offset > 0))
  const hasNext = active && (neighbour(seq!, index, 1) != null
    || (paged && seq!.offset + seq!.keys.length < seq!.total))

  // Warm the neighbours so a step paints immediately instead of flashing a
  // skeleton per call. The detail endpoint is a pure read.
  useEffect(() => {
    if (!active) return
    for (const dir of [1, -1] as const) {
      const k = neighbour(seq!, index, dir)
      if (k) {
        void qc.prefetchQuery({
          queryKey: ['intel-call', k],
          queryFn: () => api<IntelDetail>(`/intel/calls/${k}`),
        })
      }
    }
  }, [active, seq, index, qc])

  const go = useCallback(async (dir: 1 | -1) => {
    if (!seq || index < 0 || busy) return
    let s = seq
    let target = neighbour(s, index, dir)
    if (!target && s.filterQs != null) {
      // Walked off the end of the page the list handed over: fetch the
      // neighbouring page with the SAME predicates and keep going.
      const edge = dir === 1 ? s.offset + s.keys.length : s.offset - 1
      if (edge < 0 || edge >= s.total) return
      const page = Math.floor(edge / s.pageSize) + 1
      setBusy(true)
      try {
        const res = await api<{ items: IntelCallRow[]; total: number }>(
          `/intel/calls?page=${page}&limit=${s.pageSize}`
          + (s.filterQs ? `&${s.filterQs}` : ''))
        const grown = extendSequence(s, dir === 1 ? 'after' : 'before',
          res.items.map((r) => r.call_key ?? ''), res.total)
        // Nothing new came back (the list shrank under us): record that this
        // IS the end, or the button would stay live and do nothing.
        s = grown.keys.length === s.keys.length
          ? (dir === 1 ? { ...grown, total: grown.offset + grown.keys.length }
            : { ...grown, offset: 0 })
          : grown
        saveSequence(s)
        setSeq(s)
        target = neighbour(s, s.keys.indexOf(callKey), dir)
      } catch (e: any) {
        toast(`Could not load the ${dir === 1 ? 'next' : 'previous'} page of calls: ${e?.message ?? e}`, 'error')
      } finally {
        setBusy(false)
      }
    }
    if (target) nav(`/intelligence/${target}`, { replace: true })
  }, [seq, index, busy, callKey, nav, toast])

  // ← / → step through the list. Never while typing (the transcript search,
  // the audio seek bar is an <input type=range>), never with a modifier
  // (Alt+← is the browser's own Back), never under an open dialog.
  useEffect(() => {
    if (!active) return
    const h = (e: KeyboardEvent) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      if (t && (t.isContentEditable
        || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName))) return
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return
      const dir = e.key === 'ArrowRight' ? 1 : -1
      if (dir === 1 ? !hasNext : !hasPrev) return
      e.preventDefault()
      void go(dir)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [active, hasNext, hasPrev, go])

  return {
    active, busy, hasPrev, hasNext,
    /** 1-based place in the WHOLE result, and its size. */
    position: active ? seq!.offset + index + 1 : 0,
    total: active ? seq!.total : 0,
    label: seq?.label ?? '',
    /** The list this call came from — filter intact, on this call's page. */
    listUrl: active ? listUrlFor(seq, callKey) : '/intelligence',
    prev: () => go(-1),
    next: () => go(1),
  }
}

type Pager = ReturnType<typeof useCallSequence>

/** The control itself: ‹  12 of 44  ›. Renders nothing outside a sequence. */
export function CallPager({ pager }: { pager: Pager }) {
  if (!pager.active) return null
  return (
    <div role="group" aria-label={`Step through ${pager.label}`}
      className="inline-flex items-center rounded-md ring-1 ring-[var(--border)]">
      <IconButton label={`Previous call in ${pager.label} (← key)`}
        disabled={!pager.hasPrev || pager.busy} onClick={pager.prev}>
        <ChevronLeft size={16} />
      </IconButton>
      <span aria-live="polite"
        className="min-w-[4.5rem] whitespace-nowrap px-1 text-center text-xs tabular-nums text-dim">
        {pager.busy
          ? <Spinner className="mx-auto h-3.5 w-3.5" />
          : (
            <>
              <span className="font-semibold text-soft">{pager.position}</span>
              {' '}of {pager.total}
            </>
          )}
      </span>
      <IconButton label={`Next call in ${pager.label} (→ key)`}
        disabled={!pager.hasNext || pager.busy} onClick={pager.next}>
        <ChevronRight size={16} />
      </IconButton>
    </div>
  )
}
