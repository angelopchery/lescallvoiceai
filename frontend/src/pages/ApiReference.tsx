import { useEffect } from 'react'
import { ArrowLeft, Download, Printer } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useGet } from '../api/hooks/useApi'
import { Button, Card } from '../components/ui'
import { PageHeader, Table, TD, TH, THead, TR } from '../components/kit'
import ApiDocumentation, { type DocPolicy } from '../components/ApiDocumentation'

type Payload = { base_url: string; policy: DocPolicy }

/** Every endpoint on one screen. Developers want the map before the prose, and
 *  a reference that opens with three paragraphs of preamble makes them scroll
 *  past the only thing they came for. */
const INDEX: { verb: string; path: string; does: string }[] = [
  { verb: 'GET', path: '/v1/me', does: 'Check a key: permissions, limits, and the IP we see' },
  { verb: 'GET', path: '/v1/agents', does: 'Your agents, to map the `agent` field on a call' },
  { verb: 'POST', path: '/v1/contacts', does: 'Push one contact (upserts by phone number)' },
  { verb: 'POST', path: '/v1/contacts/bulk', does: 'Push up to 500, with a per-row result' },
  { verb: 'GET', path: '/v1/contacts', does: 'List contacts' },
  { verb: 'PATCH', path: '/v1/contacts/{id}', does: 'Update, or renumber without losing history' },
  { verb: 'DELETE', path: '/v1/contacts/{id}', does: 'Remove a contact; its calls are kept' },
  { verb: 'GET', path: '/v1/contact-groups', does: 'List or create groups' },
  { verb: 'GET', path: '/v1/calls', does: 'List calls — this is the sync endpoint' },
  { verb: 'GET', path: '/v1/calls/{id}', does: 'One call in full: the payload to import' },
  { verb: 'GET', path: '/v1/calls/{id}/intelligence', does: 'Summary, scores, commitments, action items' },
  { verb: 'GET', path: '/v1/calls/{id}/transcript', does: 'Speaker-tagged turns, or plain text' },
  { verb: 'GET', path: '/v1/calls/{id}/recording', does: 'Download the call audio' },
]

const VERB_TONE: Record<string, string> = {
  GET: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-300',
  POST: 'bg-mint-500/10 text-mint-700 dark:text-mint-300',
  PATCH: 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
  DELETE: 'bg-rose-500/10 text-rose-600 dark:text-rose-300',
}

/**
 * The API reference — and the thing that prints.
 *
 * PDF comes from the browser's own print pipeline rather than a server-side
 * renderer. That is a deliberate trade: this host has no PDF engine and no
 * headless browser, and adding one (weasyprint's cairo/pango stack) to a
 * 2-core box that carries live calls buys a worse-looking document than the
 * browser already makes from the print stylesheet in index.css.
 */
export default function ApiReference() {
  // The reader's OWN limits, so the reference never quotes a default at a
  // customer who was granted something else. A failed load falls back.
  const { data } = useGet<Payload>(['apikeys'], '/apikeys')
  const base = data?.base_url || window.location.origin

  // The print stylesheet keys off this class, so the app shell only disappears
  // while this page is mounted — printing any other panel is unaffected.
  useEffect(() => {
    document.body.classList.add('printing-doc')
    return () => document.body.classList.remove('printing-doc')
  }, [])

  return (
    <div data-print-root>
      <div data-print-hide>
        <PageHeader
          title="API reference"
          crumbs={[{ label: 'Developers', to: '/developers' }]}
          sub="Every endpoint, with this account’s own limits filled in"
          actions={
            <>
              <Link to="/developers">
                <Button variant="ghost"><ArrowLeft size={14} /> Keys</Button>
              </Link>
              <Button onClick={() => window.print()}>
                <Download size={14} /> Save as PDF
              </Button>
            </>
          }
        />
      </div>

      {/* Print-only masthead: a printed page has no breadcrumb to say where it
          came from, and this document gets emailed to people who were not in
          the dashboard. */}
      <div className="mb-6 hidden print:block">
        <div className="font-display text-2xl font-bold tracking-tight">Namaskall API reference</div>
        <div className="mt-1 font-mono text-xs">{base}/v1</div>
      </div>

      <Card className="mb-6 p-5 print:border-0 print:p-0">
        <div className="mb-3 flex items-baseline justify-between gap-3">
          <h2 className="font-display text-base font-bold tracking-tight">Every endpoint</h2>
          <span className="text-xs text-dim">{INDEX.length} in total</span>
        </div>
        <Table>
          <THead><TH>Method</TH><TH>Path</TH><TH>What it does</TH></THead>
          <tbody>
            {INDEX.map((e) => (
              <TR key={e.verb + e.path}>
                <TD>
                  <span className={`rounded px-1.5 py-0.5 font-mono text-2xs font-bold ${VERB_TONE[e.verb]}`}>
                    {e.verb}
                  </span>
                </TD>
                <TD><span className="break-all font-mono text-xs">{e.path}</span></TD>
                <TD><span className="text-sm text-soft">{e.does}</span></TD>
              </TR>
            ))}
          </tbody>
        </Table>
        <p className="mt-3 flex items-center gap-2 text-xs text-dim print:hidden">
          <Printer size={12} aria-hidden />
          Save as PDF prints this whole page — send it to whoever is building the integration.
        </p>
      </Card>

      <ApiDocumentation baseUrl={base} policy={data?.policy} standalone />
    </div>
  )
}
