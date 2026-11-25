import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, PlayCircle, Square, Gauge } from 'lucide-react'
import { ttsPreviewUrl } from '../api/agents'
import {
  applyQuickControls,
  type QuickApplyResult, type QuickControls, type QuickState, type RosterVoice,
} from '../api/copilot'
import { useToast } from './Toast'
import { Segmented } from './kit'
import { Badge, Button, IconButton, Spinner, cx } from './ui'

/* Copilot quick controls (RUNBOOK §67): the agent's voice and speaking speed
   by click, instead of typing a speaker name or guessing a pace number into
   the chat. Writes the same fields the Copilot would, through the same store
   path — the agent's he/she follows the chosen voice automatically. */

const LANG_BCP: Record<string, string> = { en: 'en-IN', hi: 'hi-IN', gu: 'gu-IN' }
const bcp = (code: string) => LANG_BCP[code] ?? `${code}-IN`
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const genderMark = (g: string) => (g === 'female' ? '♀' : g === 'male' ? '♂' : '')
const pronounsFor = (g: string) => (g === 'male' ? 'he/him' : g === 'female' ? 'she/her' : '—')

type GenderFilter = 'all' | 'female' | 'male'

export function VoiceSpeedControls({
  sid, data, onApplied,
}: {
  sid: string
  data: QuickControls
  onApplied: (res: QuickApplyResult) => void
}) {
  const toast = useToast()
  const state = data.state as QuickState
  const langs = state.languages ?? []
  const range = data.pace_range
  const [speaker, setSpeaker] = useState<string | null>(state.speaker)
  const [pace, setPace] = useState<Record<string, number>>(state.pace ?? {})
  const [perLang, setPerLang] = useState(() => new Set(Object.values(state.pace ?? {})).size > 1)
  const [filter, setFilter] = useState<GenderFilter>('all')
  const [playing, setPlaying] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  // A save (here or by the chat) hands us a new server state: start from it.
  useEffect(() => {
    setSpeaker(state.speaker)
    setPace(state.pace ?? {})
  }, [state.speaker, JSON.stringify(state.pace)])
  useEffect(() => () => { audioRef.current?.pause() }, [])

  const roster = data.roster.filter((v) => filter === 'all' || v.gender === filter)
  const chosen = data.roster.find((v) => v.speaker === speaker)
  const firstLang = langs.find((l) => l.code === state.default_language)?.code ?? langs[0]?.code ?? 'en'
  const allPace = pace[firstLang] ?? range.default

  const paceChanged = useMemo(
    () => langs.some((l) => Math.abs((pace[l.code] ?? 0) - (state.pace?.[l.code] ?? 0)) > 1e-6),
    [langs, pace, state.pace])
  const voiceChanged = !!speaker && speaker !== state.speaker
  const dirty = paceChanged || voiceChanged

  const play = (key: string, url: string) => {
    const a = audioRef.current
    if (playing === key && a) { a.pause(); setPlaying(null); return }
    a?.pause()
    const next = new Audio(url)
    audioRef.current = next
    next.onended = () => setPlaying(null)
    next.onerror = () => { setPlaying(null); toast('Could not play that preview', 'error') }
    setPlaying(key)
    next.play().catch(() => setPlaying(null))
  }

  const sampleUrl = (v: RosterVoice) =>
    v.sample_url ?? ttsPreviewUrl(v.speaker, 'bulbul:v3', bcp(firstLang), 1)

  const setAll = (v: number) =>
    setPace(Object.fromEntries(langs.map((l) => [l.code, v])))

  const save = async () => {
    if (!dirty || saving) return
    setSaving(true)
    try {
      const body: { speaker?: string; pace?: Record<string, number> } = {}
      if (voiceChanged && speaker) body.speaker = speaker
      if (paceChanged) body.pace = pace
      const res = await applyQuickControls(sid, body)
      onApplied(res)
      const twin = res.twin_write
      toast(
        data.target.kind === 'draft'
          ? 'Saved — the agent will be created with this voice and speed'
          : twin && !twin.applied && data.target.twin
            ? `Saved on this direction only — ${twin.reason ?? 'the other could not be updated'}`
            : 'Saved — the next call uses it',
        'success')
    } catch (e: any) {
      toast(`Could not save: ${e?.message || e}`, 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <p className="text-xs leading-relaxed text-dim">
        {data.target.kind === 'agent'
          ? <>Changes <span className="font-semibold text-soft">{data.target.display_name || data.target.slug}</span>
              {data.target.twin ? ' — both its inbound and outbound versions' : ''}. Live calls pick it up on the next call.</>
          : 'Nothing is live yet — the agent will be created with what you pick here.'}
      </p>

      {/* speaking speed */}
      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-2">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold"><Gauge size={14} /> Speaking speed</h3>
          {langs.length > 1 && (
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-dim">
              <input type="checkbox" checked={perLang}
                onChange={(e) => { setPerLang(e.target.checked); if (!e.target.checked) setAll(allPace) }}
                className="h-3.5 w-3.5 accent-mint-600" />
              Per language
            </label>
          )}
        </div>
        {(perLang ? langs : langs.slice(0, 1)).map((l) => (
          <PaceSlider
            key={l.code}
            label={perLang ? l.name : langs.length > 1 ? 'All languages' : l.name}
            value={pace[l.code] ?? range.default}
            range={range}
            onChange={(v) => (perLang ? setPace((p) => ({ ...p, [l.code]: v })) : setAll(v))}
            onPreview={speaker ? () => play(`pace:${l.code}`,
              ttsPreviewUrl(speaker, 'bulbul:v3', bcp(l.code), pace[l.code] ?? range.default)) : undefined}
            previewing={playing === `pace:${l.code}`}
          />
        ))}
        <p className="text-2xs text-dim">
          Each voice has its own natural speed — the same number sounds different on another voice. Preview after changing either.
        </p>
      </section>

      {/* voice */}
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">Voice</h3>
          <Segmented<GenderFilter>
            value={filter}
            onChange={setFilter}
            options={[{ value: 'all', label: 'All' }, { value: 'female', label: 'Female' }, { value: 'male', label: 'Male' }]}
          />
        </div>
        {state.mixed_speakers && (
          <p className="text-2xs text-amber-600 dark:text-amber-300">
            This agent uses different voices per language — picking one sets it for every language.
          </p>
        )}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {roster.map((v) => {
            const selected = v.speaker === speaker
            return (
              <div key={v.speaker}
                className={cx('relative flex items-center rounded-2xl ring-1 transition-colors',
                  selected ? 'bg-mint-400/10 ring-mint-500' : 'bg-[var(--surface-2)] ring-[var(--border)] hover:ring-[var(--border-strong)]')}>
                <button type="button" onClick={() => setSpeaker(v.speaker)} aria-pressed={selected}
                  className="flex min-w-0 flex-1 flex-col items-start gap-0.5 rounded-2xl py-2 pl-3 pr-1 text-left">
                  <span className="flex items-center gap-1 text-sm font-semibold">
                    {selected && <Check size={12} className="text-mint-600 dark:text-mint-300" />}
                    {cap(v.speaker)}
                    <span className="text-xs font-normal text-dim">{genderMark(v.gender)}</span>
                  </span>
                  <span className="truncate text-2xs text-dim">
                    {v.recommended_for.length ? `Best for ${v.recommended_for.slice(0, 3).join(', ')}` : cap(v.gender)}
                  </span>
                </button>
                <IconButton
                  label={`Play a sample of ${cap(v.speaker)}`}
                  onClick={() => play(`v:${v.speaker}`, sampleUrl(v))}
                  className="mr-1"
                >
                  {playing === `v:${v.speaker}` ? <Square size={13} /> : <PlayCircle size={15} />}
                </IconButton>
              </div>
            )
          })}
        </div>
      </section>

      {/* footer */}
      <div className="sticky bottom-0 -mx-1 flex flex-col gap-2 border-t border-[var(--border)] bg-[var(--surface-solid)] px-1 pt-3">
        <div className="flex items-center justify-between gap-2 text-xs">
          <span className="text-dim">
            Pronouns: <span className="font-semibold text-soft">{pronounsFor(chosen?.gender ?? state.gender)}</span>
            <span className="ml-1">— follows the voice</span>
          </span>
          {voiceChanged && chosen && state.gender && chosen.gender !== state.gender && (
            <Badge tone="amber">Agent's he/she will change</Badge>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" disabled={!dirty || saving}
            onClick={() => { setSpeaker(state.speaker); setPace(state.pace ?? {}) }}>
            Reset
          </Button>
          <Button size="sm" disabled={!dirty || saving} onClick={save}>
            {saving ? <Spinner className="h-3 w-3" /> : <Check size={13} />} Save
          </Button>
        </div>
      </div>
    </div>
  )
}

function PaceSlider({
  label, value, range, onChange, onPreview, previewing,
}: {
  label: string
  value: number
  range: QuickControls['pace_range']
  onChange: (v: number) => void
  onPreview?: () => void
  previewing: boolean
}) {
  const pct = ((value - range.min) / (range.max - range.min)) * 100
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="text-soft">{label}</span>
        <span className="flex items-center gap-1">
          <span className="font-mono font-semibold tabular-nums">{value.toFixed(2)}×</span>
          {onPreview && (
            <IconButton label={`Preview ${label} at ${value.toFixed(2)}×`} onClick={onPreview} className="h-7 w-7">
              {previewing ? <Square size={12} /> : <PlayCircle size={14} />}
            </IconButton>
          )}
        </span>
      </div>
      <input
        type="range"
        min={range.min}
        max={range.max}
        step={range.step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-label={`${label} speaking speed`}
        className={cx(
          'h-1.5 w-full cursor-pointer appearance-none rounded-full',
          '[&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-[var(--accent)]',
          '[&::-moz-range-thumb]:h-4 [&::-moz-range-thumb]:w-4 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-[var(--accent)]',
        )}
        style={{ background: `linear-gradient(to right, var(--accent) ${pct}%, var(--surface-2) ${pct}%)` }}
      />
      <div className="flex justify-between text-2xs text-dim">
        <span>Slower</span><span>Faster</span>
      </div>
    </div>
  )
}
