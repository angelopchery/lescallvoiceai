import { cx } from './ui'

/**
 * Namaskall lockup drawn in the bar's own colour, not dropped in as a bitmap.
 * The handset and wordmark use currentColor (the sidebar's text colour) so they
 * sit on light glass and on the navy dark theme without a boxed background.
 * The hands stay the logo gold.
 */
export function BrandMark({ collapsed = false, className }: { collapsed?: boolean; className?: string }) {
  return (
    <span
      role="img"
      aria-label="namaskall"
      className={cx('inline-flex min-w-0 items-center gap-2 text-[var(--text)]', className)}>
      <svg viewBox="0 0 64 68" className="h-8 w-8 shrink-0" aria-hidden>
        <path
          d="M11 31.5c.6-8.2 6.6-14.6 14.6-16.2 2-.4 3.8.9 4.4 2.8.7 2.1 2.8 3.2 5 2.7 8.2-1.8 16.6 2.6 19.4 10.2.6 1.7-.2 3.6-1.8 4.4-1.5.8-2.5 2.4-2.2 4.1.8 5-2.2 10.2-7 12"
          fill="none"
          stroke="currentColor"
          strokeWidth="3.6"
          strokeLinecap="round"
        />
        <g fill="#F8B800">
          <path d="M30.2 27.2c-1.5.2-2.4 1.8-2.1 3.6.5 3.4.2 8.2-.8 12.2.9-5.2 1.5-10.6 1-14.2-.3-2.2.8-3.6 2.4-3.2.9.2 1.5 1.1 1.5 2.2v22.6c-2.4-.6-5.6-2.2-8.2-4.8-3.4-3.4-5.4-7.2-5.2-10.6.2-2.6 2.2-3.6 4-.8 1.2 1.8 2.6 5.2 3.2 8.4-.8-4.6-1.6-10-1.2-14.2.3-2.6 2-3.8 3.6-3.2 1.1.4 1.8 1.6 1.8 3.2V56c-.8 1.2-1.8 2.2-3 3 0-8.2-.2-16.6.2-22.4.2-2.4 1.6-3.8 3.2-3.4z" />
          <path d="M33.8 27.2c1.5.2 2.4 1.8 2.1 3.6-.5 3.4-.2 8.2.8 12.2-.9-5.2-1.5-10.6-1-14.2.3-2.2-.8-3.6-2.4-3.2-.9.2-1.5 1.1-1.5 2.2v22.6c2.4-.6 5.6-2.2 8.2-4.8 3.4-3.4 5.4-7.2 5.2-10.6-.2-2.6-2.2-3.6-4-.8-1.2 1.8-2.6 5.2-3.2 8.4.8-4.6 1.6-10 1.2-14.2-.3-2.6-2-3.8-3.6-3.2-1.1.4-1.8 1.6-1.8 3.2V56c.8 1.2 1.8 2.2 3 3 0-8.2.2-16.6-.2-22.4-.2-2.4-1.6-3.8-3.2-3.4z" />
        </g>
      </svg>
      <span className={cx(
        'truncate font-display text-[21px] font-bold leading-none tracking-[-0.045em]',
        collapsed && 'lg:hidden',
      )}>
        namaskall
      </span>
    </span>
  )
}
