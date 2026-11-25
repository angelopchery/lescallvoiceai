import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

/** Debounce a fast-changing value (search boxes feeding server queries). */
export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return debounced
}

/**
 * A single piece of state mirrored into the URL query string (replace, not
 * push — Back never walks through keystrokes). Values equal to the fallback
 * are removed from the URL to keep it clean. Use one hook per param:
 *
 *   const [search, setSearch] = useUrlParam('q')
 *   const [page, setPage] = useUrlParam('page', '1')
 */
export function useUrlParam(key: string, fallback = ''): [string, (v: string) => void] {
  const [params, setParams] = useSearchParams()
  const value = params.get(key) ?? fallback
  const set = useCallback((v: string) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      if (!v || v === fallback) next.delete(key)
      else next.set(key, v)
      return next
    }, { replace: true })
  }, [key, fallback, setParams])
  return [value, set]
}

/**
 * Update SEVERAL url params in one navigation.
 *
 * Two `useUrlParam` setters fired in the same tick clobber each other, and the
 * first update is lost: react-router's setSearchParams passes the CURRENT
 * RENDER's params to the updater (`nextInit(searchParams)` — it closes over
 * them), so the second call recomputes from the same pre-update state and its
 * navigate() wins. That silently broke "change a filter, reset to page 1"
 * everywhere it was written as two calls — the filter change was the one
 * thrown away.
 *
 * Pass every param that changes together:
 *
 *   const patch = useUrlPatch()
 *   patch({ q: value, page: null })   // null | '' | undefined removes the key
 */
export function useUrlPatch(): (patch: Record<string, string | number | null | undefined>) => void {
  const [, setParams] = useSearchParams()
  return useCallback((patch) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      for (const [key, raw] of Object.entries(patch)) {
        const v = raw == null ? '' : String(raw)
        if (!v) next.delete(key)
        else next.set(key, v)
      }
      return next
    }, { replace: true })
  }, [setParams])
}

/** Numeric convenience over useUrlParam (page numbers etc.). */
export function useUrlNumber(key: string, fallback: number): [number, (v: number) => void] {
  const [raw, setRaw] = useUrlParam(key, String(fallback))
  const n = Number.parseInt(raw, 10)
  const value = Number.isFinite(n) && n > 0 ? n : fallback
  const set = useCallback((v: number) => setRaw(String(v)), [setRaw])
  return [value, set]
}
