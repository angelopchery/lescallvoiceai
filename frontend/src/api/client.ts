const BASE = '/api'

/** The one place an HTTP failure becomes an Error, so every caller — JSON or
    multipart — gets the same `status` / `code` / `reason` to branch on. */
export async function httpError(res: Response): Promise<Error> {
  let detail = ''
  let code = ''
  let upgrade = ''
  try {
    const body = await res.clone().json()
    // A billing refusal is a 402 whose body is FastAPI's `detail` object,
    // not a string — unwrap it so the message is the human sentence rather
    // than "[object Object]".
    const d = body?.detail
    const inner = d && typeof d === 'object' ? d : body
    detail = typeof inner?.reason === 'string' ? inner.reason
      : typeof inner?.detail === 'string' ? inner.detail
      : typeof d === 'string' ? d
      : typeof body?.reason === 'string' ? body.reason : ''
    code = typeof inner?.code === 'string' ? inner.code : ''
    upgrade = typeof inner?.upgrade === 'string' ? inner.upgrade : ''
  } catch { /* non-JSON error body */ }
  const err = new Error(detail ? `${res.status}: ${detail}` : `${res.status} ${res.statusText}`)
  // Callers need to branch on the code (e.g. 409 = the server already
  // dropped the thing you were acting on), not string-match the message.
  ;(err as Error & { status?: number }).status = res.status
  // Billing: `code` says WHY (quota_minutes, cap_agents, plan_required…) and
  // `upgrade` is where to send the user. Pages render an upgrade panel off
  // these instead of a generic failure toast — which matters because a
  // generic failure reads as "retry", and retrying a quota refusal never
  // works.
  ;(err as Error & { code?: string }).code = code
  ;(err as Error & { upgrade?: string }).upgrade = upgrade
  ;(err as Error & { reason?: string }).reason = detail
  return err
}

export async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(opts?.headers || {}) },
    ...opts,
  })
  if (!res.ok) throw await httpError(res)
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json()
  return (await res.text()) as unknown as T
}

export function sse(path: string, onMessage: (data: any) => void): () => void {
  const es = new EventSource(`${BASE}${path}`)
  es.onmessage = (e) => {
    try { onMessage(JSON.parse(e.data)) } catch { onMessage(e.data) }
  }
  return () => es.close()
}
