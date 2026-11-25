/**
 * Speak your prompt: microphone → 16 kHz mono WAV → `/api/copilot/transcribe`.
 *
 * The recording is encoded to exactly what the speech API wants, in the
 * browser. That is the whole latency story: no MediaRecorder container, no
 * server-side ffmpeg transcode, no format negotiation — the bytes that leave
 * the tab are the bytes that get uploaded to the recognizer. Five seconds of
 * speech is ~160 kB, which uploads faster than a transcode would have taken.
 *
 * It also stops listening by itself. Waiting for a user to click Stop adds a
 * second of dead air to every prompt and produces recordings with a trailing
 * silence the recognizer has to chew through, so ~1.4 s of quiet after speech
 * ends the take.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { postForm } from '../api/mutate'

export type VoiceState = 'idle' | 'starting' | 'recording' | 'transcribing'

export type VoicePromptResult = {
  text: string
  /** Exactly what the recognizer returned, in its own script. */
  raw_text?: string
  /** True when `text` was transliterated out of an Indic script. */
  romanized?: boolean
  corrections?: { from: string; to: string; kind: string }[]
  language?: string
  ms?: Record<string, number>
  empty?: boolean
}

const SR = 16_000
const SILENCE_MS = 1400          // quiet after speech that ends the take
const MIN_MS = 400               // ignore an accidental tap
const MAX_MS = 60_000            // hard ceiling
const SPEECH_RMS = 0.012         // above this counts as someone talking

export function supportsVoice(): boolean {
  return typeof navigator !== 'undefined'
    && !!navigator.mediaDevices?.getUserMedia
    && typeof (window.AudioContext || (window as any).webkitAudioContext) === 'function'
}

/** Float32 [-1,1] at `from` Hz → Int16 at 16 kHz, box-filtered. */
function toPcm16(input: Float32Array, from: number): Int16Array {
  const ratio = from / SR
  const out = new Int16Array(Math.floor(input.length / ratio))
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio)
    const end = Math.min(input.length, Math.floor((i + 1) * ratio))
    let sum = 0
    for (let j = start; j < end; j++) sum += input[j]
    const v = end > start ? sum / (end - start) : 0
    out[i] = Math.max(-1, Math.min(1, v)) * 0x7fff
  }
  return out
}

function wavBlob(pcm: Int16Array): Blob {
  const buf = new ArrayBuffer(44 + pcm.length * 2)
  const view = new DataView(buf)
  const ascii = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i))
  }
  ascii(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); ascii(8, 'WAVE')
  ascii(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true)
  view.setUint16(22, 1, true); view.setUint32(24, SR, true)
  view.setUint32(28, SR * 2, true); view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  ascii(36, 'data'); view.setUint32(40, pcm.length * 2, true)
  new Int16Array(buf, 44).set(pcm)
  return new Blob([buf], { type: 'audio/wav' })
}

export function useVoicePrompt({ onText }: { onText: (text: string) => void }) {
  const [state, setState] = useState<VoiceState>('idle')
  const [level, setLevel] = useState(0)
  const [seconds, setSeconds] = useState(0)
  const [error, setError] = useState('')
  const [last, setLast] = useState<VoicePromptResult | null>(null)

  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const nodeRef = useRef<ScriptProcessorNode | null>(null)
  const chunksRef = useRef<Float32Array[]>([])
  const startedRef = useRef(0)
  const heardRef = useRef(false)
  const quietSinceRef = useRef(0)
  const stopRef = useRef<(send: boolean) => void>(() => {})

  const teardown = useCallback(() => {
    try { nodeRef.current?.disconnect() } catch { /* already gone */ }
    try { streamRef.current?.getTracks().forEach((t) => t.stop()) } catch { /* ditto */ }
    try { void ctxRef.current?.close() } catch { /* ditto */ }
    nodeRef.current = null
    streamRef.current = null
    ctxRef.current = null
    setLevel(0)
    setSeconds(0)
  }, [])

  // A tab closed mid-take must not leave the mic light on.
  useEffect(() => () => teardown(), [teardown])

  const stop = useCallback(async (send: boolean) => {
    const chunks = chunksRef.current
    const ctx = ctxRef.current
    const rate = ctx?.sampleRate ?? SR
    const elapsed = performance.now() - startedRef.current
    chunksRef.current = []
    teardown()
    if (!send || elapsed < MIN_MS || !chunks.length) { setState('idle'); return }

    const total = chunks.reduce((n, c) => n + c.length, 0)
    const flat = new Float32Array(total)
    let off = 0
    for (const c of chunks) { flat.set(c, off); off += c.length }

    setState('transcribing')
    try {
      const form = new FormData()
      form.append('audio', wavBlob(toPcm16(flat, rate)), 'prompt.wav')
      const out = await postForm<VoicePromptResult>('/copilot/transcribe', form)
      setLast(out)
      if (out.text) onText(out.text)
      else setError("Didn't catch that — try again a bit closer to the mic.")
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setState('idle')
    }
  }, [onText, teardown])

  stopRef.current = (send: boolean) => { void stop(send) }

  const start = useCallback(async () => {
    setError('')
    setLast(null)
    setState('starting')
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
    } catch (e: any) {
      setState('idle')
      setError(e?.name === 'NotAllowedError'
        ? 'Microphone blocked — allow it for this site in your browser settings.'
        : e?.name === 'NotFoundError'
          ? 'No microphone found.'
          : `Could not open the microphone (${e?.name || e}).`)
      return
    }
    // Ask the browser to resample for us; toPcm16 covers the ones that decline.
    const Ctor = window.AudioContext || (window as any).webkitAudioContext
    let ctx: AudioContext
    try { ctx = new Ctor({ sampleRate: SR }) } catch { ctx = new Ctor() }
    const source = ctx.createMediaStreamSource(stream)
    // ScriptProcessor is deprecated but is the only capture node that works
    // without shipping a separate AudioWorklet module; the graph lives for a
    // few seconds at a time and never competes with anything else on the page.
    const node = ctx.createScriptProcessor(4096, 1, 1)
    // Routed through a silent gain node: a ScriptProcessor only runs when it
    // reaches the destination, and connecting it directly would play the
    // user's own microphone back at them.
    const mute = ctx.createGain()
    mute.gain.value = 0

    chunksRef.current = []
    startedRef.current = performance.now()
    heardRef.current = false
    quietSinceRef.current = 0

    node.onaudioprocess = (e) => {
      const buf = e.inputBuffer.getChannelData(0)
      chunksRef.current.push(new Float32Array(buf))
      let sum = 0
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
      const rms = Math.sqrt(sum / buf.length)
      setLevel((prev) => Math.max(rms * 6, prev * 0.82))
      const now = performance.now()
      setSeconds((now - startedRef.current) / 1000)
      if (rms > SPEECH_RMS) { heardRef.current = true; quietSinceRef.current = 0 }
      else if (heardRef.current) {
        if (!quietSinceRef.current) quietSinceRef.current = now
        else if (now - quietSinceRef.current > SILENCE_MS) stopRef.current(true)
      }
      if (now - startedRef.current > MAX_MS) stopRef.current(true)
    }

    source.connect(node)
    node.connect(mute)
    mute.connect(ctx.destination)
    ctxRef.current = ctx
    streamRef.current = stream
    nodeRef.current = node
    setState('recording')
  }, [])

  const toggle = useCallback(() => {
    if (state === 'recording') void stop(true)
    else if (state === 'idle') void start()
  }, [state, start, stop])

  const cancel = useCallback(() => { if (state === 'recording') void stop(false) }, [state, stop])

  // Esc abandons the take without sending it anywhere.
  useEffect(() => {
    if (state !== 'recording') return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); cancel() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [state, cancel])

  return { state, level, seconds, error, last, toggle, cancel, setError,
           supported: supportsVoice() }
}
