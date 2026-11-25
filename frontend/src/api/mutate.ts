import { api, httpError } from './client'

/** Thin helpers around the JSON `api()` fetcher for write operations. */
export const post = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body: body == null ? undefined : JSON.stringify(body) })

export const patch = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PATCH', body: body == null ? undefined : JSON.stringify(body) })

export const put = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PUT', body: body == null ? undefined : JSON.stringify(body) })

export const del = <T = any>(path: string) => api<T>(path, { method: 'DELETE' })

/** Multipart upload (no JSON Content-Type so the browser sets the boundary).
    Failures carry the server's own sentence — a 413 "the recording is too
    long" is actionable in a way that "413 Payload Too Large" is not. */
export async function postForm<T = any>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`/api${path}`, { method: 'POST', body: form })
  if (!res.ok) throw await httpError(res)
  return res.json()
}
