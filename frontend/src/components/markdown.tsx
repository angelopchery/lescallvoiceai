/**
 * The small slice of Markdown the Copilot actually writes, rendered with the
 * app's own type scale — no library, no `dangerouslySetInnerHTML`.
 *
 * The copilot has always emitted `**bold**`, bullet lists and `###` headings;
 * the chat rendered them as literal characters, so every structured answer
 * arrived as a wall of asterisks. A markdown library would work, but it comes
 * with a stylesheet (@tailwindcss/typography) that fights the closed type
 * scale, and it parses far more syntax than the copilot ever produces.
 *
 * Two deliberate omissions, both about this app's content rather than about
 * Markdown:
 *   • `_underscore_` emphasis is NOT supported. The copilot writes agent slugs
 *     (`pramukh_agent`, `outbound_lead_qualifier`) and YAML paths
 *     (`response_bank.opening_greeting`) constantly, and treating those
 *     underscores as emphasis silently eats them.
 *   • Raw HTML is never interpreted. Everything below builds React elements
 *     from matched text, so a model that emits a `<script>` renders the
 *     characters and nothing happens.
 */
import type { ReactNode } from 'react'
import { cx } from './ui'

/* Bold, italic, inline code, strikethrough, link — matched in one left-to-right
   pass so `**bold**` is never mistaken for two `*italic*` markers. */
const INLINE =
  /(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`|~~[^~\n]+~~|\[[^\]\n]+\]\([^)\s]+\))/g

/** Only schemes that cannot execute. Anything else renders as plain text. */
function safeHref(url: string): string | null {
  const u = url.trim()
  if (/^https?:\/\//i.test(u) || /^mailto:/i.test(u)) return u
  if (u.startsWith('/') && !u.startsWith('//')) return u
  return null
}

function Inline({ text }: { text: string }): ReactNode {
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  INLINE.lastIndex = 0
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    const key = `${m.index}`
    if (tok.startsWith('**')) {
      out.push(<strong key={key} className="font-semibold text-[var(--text)]">{tok.slice(2, -2)}</strong>)
    } else if (tok.startsWith('~~')) {
      out.push(<s key={key} className="opacity-70">{tok.slice(2, -2)}</s>)
    } else if (tok.startsWith('`')) {
      out.push(
        <code key={key} className="rounded bg-[var(--bg-2)] px-1 py-0.5 font-mono text-2xs">
          {tok.slice(1, -1)}
        </code>,
      )
    } else if (tok.startsWith('[')) {
      const close = tok.indexOf('](')
      const label = tok.slice(1, close)
      const href = safeHref(tok.slice(close + 2, -1))
      out.push(href
        ? <a key={key} href={href} target="_blank" rel="noreferrer"
            className="text-mint-700 underline underline-offset-2 hover:opacity-80 dark:text-mint-300">{label}</a>
        : <span key={key}>{label}</span>)
    } else {
      out.push(<em key={key}>{tok.slice(1, -1)}</em>)
    }
    last = m.index + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return <>{out}</>
}

type Block =
  | { kind: 'p'; lines: string[] }
  | { kind: 'ul' | 'ol'; items: string[] }
  | { kind: 'h'; level: number; text: string }
  | { kind: 'hr' }
  | { kind: 'quote'; lines: string[] }

const BULLET = /^\s{0,3}[-*•]\s+(.*)$/
const NUMBERED = /^\s{0,3}\d{1,2}[.)]\s+(.*)$/
const HEADING = /^\s{0,3}(#{1,4})\s+(.*)$/
const RULE = /^\s{0,3}(?:---+|___+|\*\*\*+)\s*$/
const QUOTE = /^\s{0,3}>\s?(.*)$/

function parseBlocks(src: string): Block[] {
  const blocks: Block[] = []
  for (const line of src.split('\n')) {
    const tail = blocks[blocks.length - 1]
    let m: RegExpMatchArray | null
    if (!line.trim()) { if (tail) blocks.push({ kind: 'p', lines: [] }); continue }
    if (RULE.test(line)) { blocks.push({ kind: 'hr' }); continue }
    if ((m = line.match(HEADING))) {
      blocks.push({ kind: 'h', level: m[1].length, text: m[2] }); continue
    }
    if ((m = line.match(BULLET))) {
      if (tail?.kind === 'ul') tail.items.push(m[1])
      else blocks.push({ kind: 'ul', items: [m[1]] })
      continue
    }
    if ((m = line.match(NUMBERED))) {
      if (tail?.kind === 'ol') tail.items.push(m[1])
      else blocks.push({ kind: 'ol', items: [m[1]] })
      continue
    }
    if ((m = line.match(QUOTE))) {
      if (tail?.kind === 'quote') tail.lines.push(m[1])
      else blocks.push({ kind: 'quote', lines: [m[1]] })
      continue
    }
    if (tail?.kind === 'p') tail.lines.push(line)
    else blocks.push({ kind: 'p', lines: [line] })
  }
  return blocks.filter((b) => b.kind !== 'p' || b.lines.length > 0)
}

/** Marks a block as the first rendered one so it carries no top margin —
    otherwise every bubble opens with a blank strip. */
const gap = (i: number, cls: string) => cx(i === 0 ? '' : cls)

function Blocks({ src }: { src: string }) {
  return (
    <>
      {parseBlocks(src).map((b, i) => {
        switch (b.kind) {
          case 'h':
            return (
              <div key={i} className={cx(gap(i, 'mt-3'), 'font-semibold text-[var(--text)]',
                b.level <= 2 ? 'text-base' : 'text-sm')}>
                <Inline text={b.text} />
              </div>
            )
          case 'ul':
          case 'ol': {
            const List = b.kind === 'ul' ? 'ul' : 'ol'
            return (
              <List key={i} className={cx(gap(i, 'mt-2'), 'space-y-1 pl-5',
                b.kind === 'ul' ? 'list-disc' : 'list-decimal',
                'marker:text-dim')}>
                {b.items.map((it, j) => <li key={j}><Inline text={it} /></li>)}
              </List>
            )
          }
          case 'quote':
            return (
              <blockquote key={i} className={cx(gap(i, 'mt-2'),
                'border-l-2 border-[var(--border-strong)] pl-3 text-soft')}>
                <Inline text={b.lines.join('\n')} />
              </blockquote>
            )
          case 'hr':
            return <hr key={i} className={cx(gap(i, 'mt-3'), 'border-[var(--border)]')} />
          default:
            return (
              <p key={i} className={cx(gap(i, 'mt-2'), 'whitespace-pre-wrap')}>
                <Inline text={b.lines.join('\n')} />
              </p>
            )
        }
      })}
    </>
  )
}

/**
 * Render Copilot prose. Fenced code blocks are split out first and kept
 * verbatim — a YAML snippet must survive exactly as written, which is the one
 * thing inline formatting must never be applied to.
 */
export function Markdown({ text }: { text: string }) {
  const parts = text.split('```')
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <pre key={i}
            className="mt-2 overflow-x-auto rounded-xl bg-[var(--bg-2)] px-3 py-2 font-mono text-xs leading-relaxed ring-1 ring-[var(--border)]">
            {part.replace(/^[A-Za-z0-9_-]*\n/, '').replace(/\n$/, '')}
          </pre>
        ) : (
          <Blocks key={i} src={part} />
        ),
      )}
    </>
  )
}
