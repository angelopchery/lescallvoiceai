import { useParams } from 'react-router-dom'
import { Printer } from 'lucide-react'
import { useGet } from '../api/hooks/useApi'
import { Receipt as ReceiptT, inr } from '../api/billing'
import { Button } from '../components/ui'
import { AsyncState } from '../components/kit'

/**
 * Printable GST tax invoice.
 *
 * Rendered from the SNAPSHOT stored on the payment row, not from the tenant's
 * current billing profile — an issued invoice is a legal document and editing
 * a profile afterwards must not silently rewrite it.
 *
 * `window.print()` rather than a PDF library: the browser's own print-to-PDF
 * works on desktop, phone and iPad, produces a correct A4 page, and adds
 * nothing to the bundle.
 */
export default function Receipt() {
  const { paymentId } = useParams()
  const { data, isLoading, error } = useGet<ReceiptT>(
    ['billing', 'receipt', paymentId || ''], `/billing/receipts/${paymentId}`)

  const isCredit = !!data?.credit_note_number
  const t = data?.tax

  return (
    <div className="mx-auto max-w-3xl p-6 print:p-0">
      <div className="mb-4 flex justify-end print:hidden">
        <Button onClick={() => window.print()}>
          <Printer className="h-4 w-4" /> Print / Save PDF
        </Button>
      </div>

      <AsyncState loading={isLoading} error={error} variant="text">
        {data && t && (
          <div className="rounded-2xl border border-[var(--ui-border)] bg-white p-8 text-[13px] text-zinc-900 print:rounded-none print:border-0 print:p-0">
            <div className="flex items-start justify-between">
              <div>
                <h1 className="text-xl font-bold">
                  {isCredit ? 'Credit Note' : 'Tax Invoice'}
                </h1>
                <p className="mt-1 font-mono text-sm">
                  {data.credit_note_number || data.number}
                </p>
                <p className="text-zinc-500">
                  Issued {(data.issued_at || '').slice(0, 10)}
                </p>
              </div>
              <div className="text-right">
                <p className="text-lg font-semibold">Namaskall</p>
                <p className="text-zinc-600">{data.supplier?.legal_name}</p>
                <p className="whitespace-pre-line text-zinc-600">{data.supplier?.address}</p>
                <p className="mt-1 font-mono text-xs">GSTIN {data.supplier?.gstin}</p>
              </div>
            </div>

            <div className="mt-8 grid grid-cols-2 gap-6">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Billed to</p>
                <p className="mt-1 font-medium">
                  {data.customer?.legal_name || data.customer?.owner}
                </p>
                <p className="whitespace-pre-line text-zinc-600">{data.customer?.address}</p>
                <p className="text-zinc-600">{data.customer?.owner}</p>
                {data.customer?.gstin && (
                  <p className="mt-1 font-mono text-xs">GSTIN {data.customer.gstin}</p>
                )}
              </div>
              <div className="text-right">
                <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Details</p>
                <p className="mt-1 text-zinc-600">Place of supply: {t.place_of_supply}</p>
                {data.period_start && (
                  <p className="text-zinc-600">
                    Period: {(data.period_start || '').slice(0, 10)} → {(data.period_end || '').slice(0, 10)}
                  </p>
                )}
                <p className="text-zinc-600">Payment: {data.method || '—'}</p>
                <p className="font-mono text-xs text-zinc-500">{data.gateway_payment_id}</p>
              </div>
            </div>

            <table className="mt-8 w-full border-collapse">
              <thead>
                <tr className="border-y border-zinc-300 text-xs uppercase tracking-wide text-zinc-500">
                  <th className="py-2 text-left font-semibold">Description</th>
                  <th className="py-2 text-left font-semibold">SAC</th>
                  <th className="py-2 text-right font-semibold">Amount</th>
                </tr>
              </thead>
              <tbody>
                {data.line_items.map((li, i) => (
                  <tr key={i} className="border-b border-zinc-200">
                    <td className="py-3">{li.description}</td>
                    <td className="py-3 font-mono text-xs">{li.sac}</td>
                    <td className="py-3 text-right tabular-nums">{inr(li.amount_paise, { paise: true })}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="mt-4 flex justify-end">
              <table className="w-72">
                <tbody className="text-zinc-700">
                  <tr>
                    <td className="py-1">Taxable value</td>
                    <td className="py-1 text-right tabular-nums">{inr(t.base_paise, { paise: true })}</td>
                  </tr>
                  {t.inter_state ? (
                    <tr>
                      <td className="py-1">IGST @ 18%</td>
                      <td className="py-1 text-right tabular-nums">{inr(t.igst_paise, { paise: true })}</td>
                    </tr>
                  ) : (
                    <>
                      <tr>
                        <td className="py-1">CGST @ 9%</td>
                        <td className="py-1 text-right tabular-nums">{inr(t.cgst_paise, { paise: true })}</td>
                      </tr>
                      <tr>
                        <td className="py-1">SGST @ 9%</td>
                        <td className="py-1 text-right tabular-nums">{inr(t.sgst_paise, { paise: true })}</td>
                      </tr>
                    </>
                  )}
                  <tr className="border-t border-zinc-300 font-semibold text-zinc-900">
                    <td className="py-2">Total</td>
                    <td className="py-2 text-right tabular-nums">{inr(t.total_paise, { paise: true })}</td>
                  </tr>
                  {data.refunded_paise > 0 && (
                    <tr className="text-amber-700">
                      <td className="py-1">Refunded</td>
                      <td className="py-1 text-right tabular-nums">
                        −{inr(data.refunded_paise, { paise: true })}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <p className="mt-10 border-t border-zinc-200 pt-4 text-xs text-zinc-500">
              This is a computer-generated {isCredit ? 'credit note' : 'invoice'} and
              does not require a signature. Tax payable on reverse charge: No.
            </p>
          </div>
        )}
      </AsyncState>
    </div>
  )
}
