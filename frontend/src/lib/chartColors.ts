/**
 * The only source of chart colors. Values resolve through the --chart-* CSS
 * variables (theme-aware, defined in index.css) — never hardcode hex in a
 * chart again.
 *
 * Semantic note: in dense multi-series charts `hot` uses amber so it stays
 * distinguishable from `negative` (rose) — the old hardcoded palette rendered
 * both as the identical #fb7185. StatusBadge keeps hot=rose for single pills;
 * that's a different context and never sits in the same series.
 */
export const CHART = {
  mint: 'hsl(var(--chart-1))',
  indigo: 'hsl(var(--chart-2))',
  amber: 'hsl(var(--chart-3))',
  rose: 'hsl(var(--chart-4))',
  neutral: 'hsl(var(--chart-5))',
} as const

/** Categorical order for unnamed series. */
export const SERIES: string[] = [CHART.mint, CHART.indigo, CHART.amber, CHART.rose, CHART.neutral]

/** Sentiment / lead-temperature series colors. */
export const TONE: Record<string, string> = {
  positive: CHART.mint,
  negative: CHART.rose,
  hot: CHART.amber,
  warm: CHART.amber,
  cold: CHART.indigo,
  neutral: CHART.neutral,
}

/** One threshold law for 0-100 scores (lead/opportunity; risk is inverted). */
export function scoreColor(score: number, invert = false): string {
  const s = invert ? 100 - score : score
  if (s >= 70) return CHART.mint
  if (s >= 40) return CHART.amber
  return CHART.rose
}
