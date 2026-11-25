export const inr = (n: number | null | undefined, dp = 0): string => {
  if (n == null || isNaN(Number(n))) return '₹0'
  const v = Number(n)
  return '₹' + v.toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

export const num = (n: number | null | undefined): string => {
  if (n == null || isNaN(Number(n))) return '0'
  return Number(n).toLocaleString('en-IN')
}

export const compact = (n: number | null | undefined): string => {
  if (n == null) return '0'
  return Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(n))
}

export const dur = (sec: number | null | undefined): string => {
  if (!sec || sec < 0) return '0:00'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export const pct = (n: number | null | undefined, dp = 0): string => {
  if (n == null) return '0%'
  return `${Number(n).toFixed(dp)}%`
}

export const clockTime = (d = new Date()): string =>
  d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' })

/* ------------------------------------------------------------------ Time

   Server timestamps reach us in three different shapes, because the DB layer
   hands back Postgres' raw text form (shared/db/database.py::_TextLoader):

     "2026-08-06 06:45:11"       naive TIMESTAMP  — calls.started_at/ended_at,
                                 contacts, campaigns … everything in schema.sql
     "2026-08-06 06:45:11+00"    TIMESTAMPTZ      — the intel/followup tables
     "2026-08-06T06:45:11Z"      real ISO         — endpoints that use _as_iso()

   Only the third is safe to hand to `new Date()`. The naive form is parsed as
   LOCAL time by every JS engine, so a UTC instant shown to a viewer in IST came
   out 5h30m early — which is exactly why call times read as stale. The "+00"
   form is not strict ISO either: V8 tolerates it, JavaScriptCore does not, so
   it renders as "Invalid Date" in Safari.

   Everything that displays a recorded time must parse through parseTs(). It
   pins naive strings to UTC — the platform stores UTC end to end (the host,
   Postgres and the runtime all run UTC) — and rewrites the offset into a form
   every engine agrees on. Output is then rendered in the VIEWER's timezone.
*/

/** Server timestamp -> a real instant, or null when absent/unparseable. */
export function parseTs(value: string | number | Date | null | undefined): Date | null {
  if (value == null || value === '') return null
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') {
    // Bare epochs: seconds below the year-2001-in-ms boundary, else millis.
    // A zero/negative epoch is an absent field, not 1970 — never render it.
    if (!Number.isFinite(value) || value <= 0) return null
    const d = new Date(value < 1e12 ? value * 1000 : value)
    return isNaN(d.getTime()) ? null : d
  }

  let s = String(value).trim()
  if (!s) return null

  // A date-only value is a calendar day, not an instant. Anchoring it to local
  // midnight keeps "6 Aug" labelled 6 Aug; parsing it as UTC would slide it
  // into the previous day for any viewer behind UTC.
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    const [y, m, d] = s.split('-').map(Number)
    return new Date(y, m - 1, d)
  }

  s = s.replace(' ', 'T')
  // Look for the zone designator in the TIME half only — the date half's
  // hyphens would otherwise read as a negative offset.
  if (/(?:Z|[+-]\d{2}(?::?\d{2})?)$/i.test(s.slice(11))) {
    s = s.replace(/([+-]\d{2})$/, '$1:00')  // "+00" -> "+00:00"
  } else {
    s += 'Z'                                 // naive is UTC, platform-wide
  }

  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}

const DASH = '—'

/** "06 Aug, 12:15 pm" — the default rendering for a recorded instant. */
export const fmtDateTime = (v: Parameters<typeof parseTs>[0], seconds = false): string => {
  const d = parseTs(v)
  if (!d) return DASH
  return d.toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    ...(seconds ? { second: '2-digit' as const } : {}),
  })
}

/** "12:15 pm" — when the surrounding row already establishes the day. */
export const fmtTime = (v: Parameters<typeof parseTs>[0], seconds = false): string => {
  const d = parseTs(v)
  if (!d) return DASH
  return d.toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit',
    ...(seconds ? { second: '2-digit' as const } : {}),
  })
}

/** "06 Aug 2026" — a calendar date with no clock.
 *  Note this is the date IN THE VIEWER'S ZONE: slicing the stored string
 *  instead would report the UTC date, which is the previous day for anything
 *  recorded after 18:30 UTC. */
export const fmtDate = (v: Parameters<typeof parseTs>[0]): string => {
  const d = parseTs(v)
  if (!d) return DASH
  return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

/** Full local rendering, incl. zone — for `title` tooltips on a short label. */
export const fmtFull = (v: Parameters<typeof parseTs>[0]): string => {
  const d = parseTs(v)
  return d ? d.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'long' }) : DASH
}

/** 'Today' / 'Yesterday' / '6 Aug' (with the year once it isn't the current one). */
export const fmtDayLabel = (v: Parameters<typeof parseTs>[0]): string => {
  const d = parseTs(v)
  if (!d) return DASH
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const now = new Date()
  const diff = Math.round((startOf(now) - startOf(d)) / 86_400_000)
  if (diff === 0) return 'Today'
  if (diff === 1) return 'Yesterday'
  return d.toLocaleDateString('en-IN', {
    day: 'numeric', month: 'short',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' as const } : {}),
  })
}
