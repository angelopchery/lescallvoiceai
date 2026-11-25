/**
 * Motion layer — animated primitives shared across panels.
 *
 * CountingNumber is adapted from Animate UI (animate-ui.com, MIT).
 * BorderBeam and TextShimmer are React ports of Inspira UI effects
 * (inspira-ui.com, MIT — the upstream is Vue-only, so the effects are
 * ported here on the app's own tokens rather than installed).
 *
 * Reduced motion: Motion components inherit MotionConfig
 * reducedMotion="user" (main.tsx); the CSS effects are silenced by the
 * global prefers-reduced-motion rule in index.css.
 */
import { useEffect, useRef } from 'react'
import { useInView, useMotionValue, useReducedMotion, useSpring } from 'motion/react'
import { cx } from './ui'

const enIN = (v: number) => Math.round(v).toLocaleString('en-IN')

/**
 * Spring-animated number for metric values. Counts up on first view and
 * re-springs when a poll delivers a new value; under reduced motion it
 * renders the final number immediately.
 */
export function CountingNumber({ value, format = enIN, className }: {
  value: number
  /** Formats every animation frame — defaults to en-IN integer grouping. */
  format?: (v: number) => string
  className?: string
}) {
  const ref = useRef<HTMLSpanElement>(null)
  // Latest formatter without re-subscribing the spring on every render.
  const fmtRef = useRef(format)
  fmtRef.current = format
  const reduced = useReducedMotion()
  const mv = useMotionValue(0)
  const spring = useSpring(mv, { stiffness: 90, damping: 28 })
  const inView = useInView(ref, { once: true, margin: '0px 0px -10% 0px' })

  useEffect(() => {
    if (!inView) return
    if (reduced) spring.jump(value)
    mv.set(value)
  }, [inView, value, reduced, mv, spring])

  useEffect(() => spring.on('change', (v) => {
    if (ref.current) ref.current.textContent = fmtRef.current(v)
  }), [spring])

  return (
    <span ref={ref} className={cx('tabular-nums', className)}>
      {format(reduced ? value : 0)}
    </span>
  )
}

/**
 * Animated beam tracing the border of its nearest positioned ancestor —
 * drop inside a `.card` (relative + overflow-hidden, radius inherits).
 * Use once per screen at most: it marks THE thing to look at.
 */
export function BorderBeam({ colorFrom = 'var(--accent)', colorTo = 'var(--accent-2)', duration = 7, className }: {
  colorFrom?: string; colorTo?: string; duration?: number; className?: string
}) {
  return (
    <span
      aria-hidden
      className={cx('border-beam', className)}
      style={{
        '--beam-from': colorFrom,
        '--beam-to': colorTo,
        '--beam-duration': `${duration}s`,
      } as React.CSSProperties}
    />
  )
}

/** Shimmering text for in-progress states ("Thinking…") — reads as alive
 *  without a spinner. Falls back to static gradient text under reduced motion. */
export function TextShimmer({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cx('text-shimmer', className)}>{children}</span>
}
