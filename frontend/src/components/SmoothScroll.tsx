import { useEffect, useState, type ReactNode } from 'react'
import { ReactLenis } from 'lenis/react'

/**
 * Lenis smooth scrolling (lenis.darkroom.engineering) on the window scroller.
 * Nested scrollers (sidebar nav, chat logs, modal bodies) keep native wheel
 * behavior via allowNestedScroll + data-lenis-prevent on the known ones.
 * Users who ask for reduced motion get native scrolling — Lenis is simply
 * not mounted for them.
 */
export function SmoothScroll({ children }: { children: ReactNode }) {
  const [reduced, setReduced] = useState(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  )
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  if (reduced) return <>{children}</>
  return (
    // syncTouch stays off: phones and iPads keep native touch scrolling
    // (rubber-banding, momentum) — Lenis only smooths mouse wheels.
    <ReactLenis root options={{ lerp: 0.14, smoothWheel: true, syncTouch: false, anchors: true, allowNestedScroll: true }}>
      {children}
    </ReactLenis>
  )
}
