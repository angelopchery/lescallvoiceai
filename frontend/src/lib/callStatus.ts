/**
 * One vocabulary for call status across the app.
 *
 * `empty_call` is the state a dial result alone cannot express: the callee
 * picked up, so the leg is "answered" as far as FreeSWITCH is concerned, but no
 * real conversation happened — silence, noise only, a voicemail greeting, or an
 * instant hang-up. It is computed at teardown (calls.answer_state) rather than
 * from the originate result, and folded back over the phone book at read time
 * (server.py `_empty_call_contacts`) so it filters like any other status.
 *
 * `not_called` stays page-specific: it describes a CONTACT that was never
 * dialled, which only the phone book knows about, so it has no meaning in the
 * activity feed.
 */
export type CallStatusValue =
  | 'all' | 'not_called' | 'answered' | 'empty_call'
  | 'no_answer' | 'declined' | 'invalid_number' | 'busy' | 'failed'

export const CALL_STATUS_LABEL: Record<CallStatusValue, string> = {
  all: 'All',
  not_called: 'Not called',
  answered: 'Answered',
  empty_call: 'Empty call',
  no_answer: 'No answer',
  declined: 'Declined',
  // The carrier could not route the NUMBER (UNALLOCATED_NUMBER / no route):
  // nobody was reached and nobody refused. It used to be folded into
  // 'declined', which is also what a human pressing reject looks like.
  invalid_number: 'Invalid number',
  busy: 'Busy',
  failed: 'Failed',
}

const opt = (value: CallStatusValue) => ({ value, label: CALL_STATUS_LABEL[value] })

/**
 * Phone-book filter (Outbound): contacts, so `not_called` applies.
 *
 * `failed` belongs here too — `_parse_fs_result` returns it as the DEFAULT for
 * any originate it cannot classify, so contacts genuinely carry it, and the
 * page has always shown a "Failed" stat card counting them. Leaving it out of
 * the filter made that card the only place those contacts appeared: a number
 * you could see but never click through to.
 */
export const CONTACT_STATUS_FILTER = [
  opt('all'), opt('not_called'), opt('answered'), opt('empty_call'),
  opt('no_answer'), opt('declined'), opt('invalid_number'), opt('busy'), opt('failed'),
] as const

/** Call-feed filter (Activity): real calls, so `not_called` does not apply. */
export const CALL_STATUS_FILTER = [
  opt('all'), opt('answered'), opt('empty_call'), opt('no_answer'),
  opt('declined'), opt('invalid_number'), opt('busy'), opt('failed'),
] as const

/* ------------------------------------------------------------------ Lead disposition */

/**
 * What a human (or the auto-classifier) decided about the LEAD, as opposed to
 * what the phone did. It lives next to call status because the outbound page
 * treats the two as one vocabulary: each names an audience you might dial, and
 * the campaign engine matches on either (`_campaign_audience`).
 */
export type DispositionValue =
  | 'not_classified' | 'interested' | 'site_visit_booked' | 'inquiry'
  | 'in_doubt' | 'to_contact_later' | 'rejected'

export const DISPOSITION_LABEL: Record<DispositionValue, string> = {
  not_classified: 'Not classified',
  interested: 'Interested',
  site_visit_booked: 'Site visit booked',
  inquiry: 'Inquiry',
  in_doubt: 'In doubt',
  to_contact_later: 'To contact later',
  rejected: 'Rejected',
}

/** Every disposition, for the per-row editor. */
export const DISPOSITION_OPTIONS = (Object.keys(DISPOSITION_LABEL) as DispositionValue[])
  .map((value) => ({ value, label: DISPOSITION_LABEL[value] }))

/** Disposition as a FILTER — 'all' first, and Rejected included: writing a
 *  lead off must not make it unfindable. */
export const DISPOSITION_FILTER = [
  { value: 'all', label: 'Any disposition' }, ...DISPOSITION_OPTIONS,
] as const

/** Disposition as a campaign AUDIENCE. Rejected is absent on purpose: those
 *  contacts asked not to be called, the engine refuses them, and offering the
 *  option only produces a run that queues nothing. */
export const DISPOSITION_AUDIENCE = DISPOSITION_FILTER
  .filter((o) => o.value !== 'rejected')

/** Call status as a campaign AUDIENCE — the same list the filter row shows, so
 *  anything you can narrow to is something you can dial. */
export const STATUS_AUDIENCE = CONTACT_STATUS_FILTER.map((o) =>
  ({ value: o.value, label: o.value === 'all' ? 'Any call status' : o.label }))

/** Human name for any status/disposition key, for confirms and toasts. */
export const audienceLabel = (v?: string | null): string => {
  if (!v) return '—'
  if (v === 'all') return 'Everyone'
  if (v === 'selection') return 'Selected contacts'
  return CALL_STATUS_LABEL[v as CallStatusValue]
    ?? DISPOSITION_LABEL[v as DispositionValue]
    ?? String(v).replace(/_/g, ' ')
}
