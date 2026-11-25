import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Braces, Building2, ChevronRight, CreditCard, Mail, PhoneCall, Save, User } from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { put } from '../api/mutate'
import { Button, Card, SectionHead, Spinner } from '../components/ui'
import { PageHeader, Field, Input, AsyncState } from '../components/kit'
import { Avatar, AvatarFallback, AvatarImage } from '../components/ui/avatar'
import { useToast } from '../components/Toast'

/**
 * The account page — who you are, and where to go for the rest.
 *
 * It used to carry the entire integration API behind a two-item tab strip:
 * key management, limits, usage and a full reference manual, all inside a
 * *settings* page. That has moved to /developers, which is a destination in its
 * own right. What is left here is short on purpose — this is a page people
 * visit twice a year, and it should not pretend otherwise.
 */

type Profile = {
  email: string
  name: string
  phone: string
  company: string
  picture?: string
}

/** A signpost to a neighbouring page. Two of these replace what used to be a
 *  tab strip: a link is honest about leaving, a tab is not. */
function Signpost({ to, icon, title, detail }: {
  to: string; icon: React.ReactNode; title: string; detail: string
}) {
  return (
    <Link to={to}
      className="group flex items-center gap-3 rounded-xl px-3 py-2.5 transition-colors hover:bg-[var(--surface-2)]">
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-[var(--surface-2)] text-soft ring-1 ring-[var(--border)] group-hover:text-[var(--text)]">
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-semibold">{title}</span>
        <span className="block text-xs text-dim">{detail}</span>
      </span>
      <ChevronRight size={15} className="shrink-0 text-dim transition-transform group-hover:translate-x-0.5" />
    </Link>
  )
}

export default function Settings() {
  const { data, isLoading, error } = useGet<Profile>(['profile'], '/auth/profile')
  const refresh = useRefresh()
  const toast = useToast()
  const [name, setName] = useState('')
  const [phone, setPhone] = useState('')
  const [company, setCompany] = useState('')
  const [saving, setSaving] = useState(false)
  const [seeded, setSeeded] = useState(false)

  // Seed the form once from the server; afterwards the user's edits win.
  useEffect(() => {
    if (!data || seeded) return
    setName(data.name || '')
    setPhone(data.phone ? `+${data.phone}` : '')
    setCompany(data.company || '')
    setSeeded(true)
  }, [data, seeded])

  // Save stays disabled until something actually differs from the server copy —
  // an always-enabled Save invites no-op writes and reads as "did that work?".
  const dirty = useMemo(() => {
    if (!data) return false
    return name !== (data.name || '')
      || phone !== (data.phone ? `+${data.phone}` : '')
      || company !== (data.company || '')
  }, [data, name, phone, company])

  const save = async () => {
    setSaving(true)
    try {
      const res = await put<Profile & { status: string }>('/auth/profile', { name, phone, company })
      toast('Profile saved', 'success')
      setPhone(res.phone ? `+${res.phone}` : '')
      // ['auth','me'] backs the account chip in the sidebar — without this the
      // name you just saved keeps showing the one Google supplied at login
      // until the next full reload.
      refresh(['profile'], ['auth', 'me'])
    } catch (e: any) {
      toast(`Could not save: ${e.message || e}`, 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <PageHeader title="Settings" sub="Your account details, used across the platform" />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,620px)_minmax(0,1fr)]">
        <Card className="p-6">
          <AsyncState loading={isLoading} error={error} variant="text" rows={7}>
            <div className="flex items-center gap-3.5 border-b border-[var(--border)] pb-5">
              <Avatar className="h-12 w-12">
                {data?.picture && <AvatarImage src={data.picture} alt="" referrerPolicy="no-referrer" />}
                <AvatarFallback className="bg-gradient-to-br from-indigo-400 to-indigo-600 text-lg font-bold text-white">
                  {(name || data?.email || 'A').trim().charAt(0).toUpperCase()}
                </AvatarFallback>
              </Avatar>
              <div className="min-w-0">
                <div className="truncate font-display text-base font-bold tracking-tight">
                  {name || 'Your account'}
                </div>
                <div className="flex min-w-0 items-center gap-1.5 text-xs text-dim">
                  <Mail size={12} className="shrink-0" aria-hidden />
                  <span className="truncate">{data?.email}</span>
                </div>
              </div>
            </div>

            <div className="space-y-4 pt-5">
              <Field label="Full name">
                <div className="relative">
                  <User size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
                  <Input value={name} onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Angelo Fernandes" className="pl-9" />
                </div>
              </Field>

              <Field label="Your phone number">
                <div className="relative">
                  <PhoneCall size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
                  <Input value={phone} onChange={(e) => setPhone(e.target.value)}
                    placeholder="+91 98765 43210" inputMode="tel" className="pl-9" />
                </div>
                <p className="mt-1 text-xs text-dim">
                  Include the country code. “Test call” on your numbers dials <i>this</i> phone,
                  from your own number, with your agent live on the line.
                </p>
              </Field>

              <Field label="Company (optional)">
                <div className="relative">
                  <Building2 size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
                  <Input value={company} onChange={(e) => setCompany(e.target.value)}
                    placeholder="e.g. Dhronai Technologies" className="pl-9" />
                </div>
                <p className="mt-1 text-xs text-dim">
                  Shown on your account, and available to your agents as context.
                </p>
              </Field>

              <div className="flex items-center gap-3 border-t border-[var(--border)] pt-4">
                <Button onClick={save} disabled={saving || !dirty}>
                  {saving ? <Spinner className="h-3.5 w-3.5" /> : <Save size={14} />}
                  {saving ? 'Saving…' : 'Save changes'}
                </Button>
                {dirty && !saving && <span className="text-xs text-dim">Unsaved changes</span>}
              </div>
            </div>
          </AsyncState>
        </Card>

        <Card className="h-fit p-4">
          <SectionHead title="Elsewhere" sub="Account settings that live on their own pages" />
          <div className="-mx-1 space-y-0.5">
            <Signpost
              to="/developers" icon={<Braces size={16} />}
              title="Developers"
              detail="API keys, limits and the full reference for connecting your CRM"
            />
            <Signpost
              to="/billing" icon={<CreditCard size={16} />}
              title="Billing"
              detail="Your plan, minutes used, invoices and receipts"
            />
          </div>
        </Card>
      </div>
    </div>
  )
}
