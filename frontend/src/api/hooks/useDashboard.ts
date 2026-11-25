import { useQuery } from '@tanstack/react-query'
import { api } from '../client'
import { demoHealth } from '../demo'

export function useHealth() {
  const q = useQuery({
    queryKey: ['health'],
    queryFn: () => api<any>('/health'),
    refetchInterval: 20_000,
  })
  // /health is a runtime snapshot (no active-call count); poll that separately.
  const live = useQuery({
    queryKey: ['active-count'],
    queryFn: () => api<any>('/calls/active'),
    refetchInterval: 10_000,
  })
  const liveCount = live.data?.count ?? live.data?.active_calls?.length ?? 0
  if (!q.data) return { ...demoHealth, liveCount }
  const services = normalizeServices(q.data.services)
  const overall = (q.data.overall as 'healthy' | 'degraded' | 'down') ?? demoHealth.overall
  return { services, overall, liveCount }
}

/**
 * Backend /api/health returns `services` as an object keyed by name
 * ({ ws_audio_receiver: { running: true }, ... }), but older builds expected an
 * array. Accept either shape so a single unexpected payload can't blank the app.
 */
function normalizeServices(raw: any) {
  if (Array.isArray(raw)) {
    return raw.map((s: any) => ({ name: s.name ?? s.service, ok: !!(s.ok ?? s.running) }))
  }
  if (raw && typeof raw === 'object') {
    return Object.entries(raw).map(([name, v]: [string, any]) => ({
      name,
      ok: !!(v?.ok ?? v?.running),
    }))
  }
  return demoHealth.services
}
