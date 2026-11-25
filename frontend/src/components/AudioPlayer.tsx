import { useEffect, useRef, useState } from 'react'
import { Pause, Play, RotateCcw } from 'lucide-react'
import { Badge, Spinner, cx } from './ui'

/**
 * Module-level coordination: only one recording plays at a time. Starting a
 * player pauses whichever other instance was active.
 */
let activeEl: HTMLAudioElement | null = null

function fmtTime(sec: number): string {
  if (!isFinite(sec) || sec < 0) return '--:--'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

/**
 * Themed inline audio player; lazy-loads the WAV only on first play.
 * Renders a mint play/pause control, a scrubber and a time readout instead of
 * the browser's stock chrome, with buffering + error feedback.
 */
export function AudioPlayer({ src, caller }: { src: string; caller?: string }) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)
  const [time, setTime] = useState(0)
  const [duration, setDuration] = useState(0)

  // Release the shared active-player slot if we unmount while holding it.
  useEffect(() => {
    const el = audioRef.current
    return () => {
      if (el && activeEl === el) activeEl = null
    }
  }, [])

  const who = caller ? ` recording from ${caller}` : ' recording'

  const retry = () => {
    const el = audioRef.current
    if (!el) return
    setFailed(false)
    setLoading(true)
    el.load()
    void el.play().catch((err: unknown) => {
      if ((err as DOMException)?.name === 'AbortError') return
      setLoading(false)
      setFailed(true)
    })
  }

  const toggle = () => {
    const el = audioRef.current
    if (!el) return
    if (failed) {
      retry()
      return
    }
    if (el.paused) {
      setLoading(true) // fetch starts now (preload="none")
      void el.play().catch((err: unknown) => {
        if ((err as DOMException)?.name === 'AbortError') return
        setLoading(false)
        setFailed(true)
      })
    } else {
      el.pause()
    }
  }

  const onSeek = (v: number) => {
    const el = audioRef.current
    if (!el) return
    el.currentTime = v
    setTime(v)
  }

  const dur = isFinite(duration) && duration > 0 ? duration : 0
  const pct = dur > 0 ? Math.min(100, (time / dur) * 100) : 0

  return (
    <div className="flex w-full min-w-[190px] max-w-[280px] items-center gap-2">
      <audio
        ref={audioRef}
        preload="none"
        src={src}
        onPlay={(e) => {
          const el = e.currentTarget
          if (activeEl && activeEl !== el) activeEl.pause()
          activeEl = el
          setPlaying(true)
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        // NOTE: no onLoadStart — with preload="none" browsers fire it at mount
        // and canplay never follows, which froze every row in a spinner state.
        // Loading is set explicitly in toggle()/retry() when a fetch begins.
        onWaiting={() => setLoading(true)}
        onCanPlay={() => setLoading(false)}
        onPlaying={() => { setLoading(false); setFailed(false) }}
        onLoadedMetadata={(e) => {
          const d = e.currentTarget.duration
          if (isFinite(d)) setDuration(d)
        }}
        onDurationChange={(e) => {
          const d = e.currentTarget.duration
          if (isFinite(d)) setDuration(d)
        }}
        onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        onError={() => { setLoading(false); setPlaying(false); setFailed(true) }}
      />

      <button
        onClick={toggle}
        aria-label={failed ? `Retry${who}` : playing ? `Pause${who}` : `Play${who}`}
        title={failed ? 'Retry' : playing ? 'Pause' : 'Play'}
        className="grid h-9 w-9 shrink-0 place-items-center rounded-full sm:h-8 sm:w-8 bg-mint-400/15 text-mint-600 ring-1 ring-mint-400/30 transition hover:bg-mint-400/25 active:scale-[.97] dark:text-mint-300"
      >
        {loading ? (
          <Spinner className="h-3.5 w-3.5" />
        ) : playing ? (
          <Pause size={14} />
        ) : (
          <Play size={14} className="ml-0.5" />
        )}
      </button>

      {failed ? (
        <div className="flex items-center gap-2">
          <Badge tone="rose">Audio unavailable</Badge>
          <button
            onClick={retry}
            aria-label={`Retry loading${who}`}
            title="Retry"
            className="-m-2 shrink-0 p-2 text-dim transition-colors hover:text-soft"
          >
            <RotateCcw size={12} />
          </button>
        </div>
      ) : (
        <>
          <input
            type="range"
            min={0}
            max={dur || 1}
            step={0.1}
            value={Math.min(time, dur || 0)}
            disabled={!dur}
            onChange={(e) => onSeek(Number(e.target.value))}
            aria-label={`Seek${who}`}
            className={cx(
              'h-1.5 w-full appearance-none rounded-full',
              dur ? 'cursor-pointer' : 'cursor-default',
              '[&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-[var(--accent)]',
              '[&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-3 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-[var(--accent)]',
            )}
            style={{ background: `linear-gradient(to right, var(--accent) ${pct}%, var(--surface-2) ${pct}%)` }}
          />
          <span className="shrink-0 whitespace-nowrap text-2xs tabular-nums text-dim">
            {fmtTime(time)} / {dur ? fmtTime(dur) : '--:--'}
          </span>
        </>
      )}
    </div>
  )
}
