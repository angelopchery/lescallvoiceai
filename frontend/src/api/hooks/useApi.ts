import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../client'

/**
 * Generic GET hook. `key` namespaces the cache; `path` is the /api path.
 * Pass refetchInterval for polling endpoints, enabled:false to defer.
 */
export function useGet<T = any>(
  key: (string | number | boolean | null | undefined)[],
  path: string,
  opts?: { refetchInterval?: number; enabled?: boolean },
) {
  return useQuery<T>({
    queryKey: key,
    queryFn: () => api<T>(path),
    refetchInterval: opts?.refetchInterval,
    enabled: opts?.enabled ?? true,
  })
}

/** Returns a function that invalidates one or more query-key prefixes. */
export function useRefresh() {
  const qc = useQueryClient()
  return (...prefixes: (string | number)[][]) => {
    if (!prefixes.length) return qc.invalidateQueries()
    prefixes.forEach((p) => qc.invalidateQueries({ queryKey: p }))
  }
}
