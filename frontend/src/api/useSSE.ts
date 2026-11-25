import { useEffect, useRef, useState } from 'react'

type Status = 'connecting' | 'open' | 'error'

/**
 * Subscribe to a Server-Sent Events endpoint. Parsed JSON events are delivered
 * to `onEvent`. Pass path=null to disconnect. EventSource auto-reconnects; we
 * surface a status so the UI can show a live/reconnecting indicator.
 */
export function useSSE(
  path: string | null,
  onEvent: (data: any) => void,
  deps: any[] = [],
) {
  const [status, setStatus] = useState<Status>('connecting')
  const cb = useRef(onEvent)
  cb.current = onEvent

  useEffect(() => {
    if (!path) return
    setStatus('connecting')
    const es = new EventSource(`/api${path}`)
    es.onopen = () => setStatus('open')
    es.onerror = () => setStatus('error')
    es.onmessage = (e) => {
      let data: any = e.data
      try { data = JSON.parse(e.data) } catch { /* keep raw string */ }
      cb.current(data)
    }
    return () => es.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps])

  return status
}
