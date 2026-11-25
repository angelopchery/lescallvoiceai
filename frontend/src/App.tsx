import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate, useParams } from 'react-router-dom'
import { AppShell } from './layout/AppShell'
import Overview from './pages/Overview'
import Placeholder from './pages/Placeholder'

/**
 * Landing spot for a shared-agent link: /s/<token> → the Agents panel with the
 * import popup open.
 *
 * A single path segment with no query string is deliberate. The nginx login
 * wall bounces unauthenticated visitors through /login?next=$request_uri, and
 * stock nginx cannot URL-encode that value — so a destination carrying its own
 * `&` would be truncated. `/s/<token>` has nothing to truncate, which is what
 * makes a share link survive being opened by someone who is not logged in yet.
 */
function ShareLanding() {
  const { token = '' } = useParams()
  return <Navigate to={`/build/agents?import=${encodeURIComponent(token)}`} replace />
}

/* Only the homepage ships in the entry chunk; every other panel is fetched on
   first navigation. One 740 kB bundle meant a cold visitor downloaded the agent
   builder, the charts and the intelligence hub before seeing their own dashboard. */
const CopilotChat = lazy(() => import('./pages/CopilotChat'))
const Live = lazy(() => import('./pages/Live'))
const Activity = lazy(() => import('./pages/Activity'))
const Recordings = lazy(() => import('./pages/Recordings'))
const Transcripts = lazy(() => import('./pages/Transcripts'))
const ConvAnalytics = lazy(() => import('./pages/ConvAnalytics'))
const Intelligence = lazy(() => import('./pages/Intelligence'))
const IntelligenceCall = lazy(() => import('./pages/IntelligenceCall'))
const FollowUps = lazy(() => import('./pages/FollowUps'))
const Reports = lazy(() => import('./pages/Reports'))
const Outbound = lazy(() => import('./pages/Outbound'))
const Contacts = lazy(() => import('./pages/Contacts'))
const ContactHub = lazy(() => import('./pages/ContactHub'))
const Telephony = lazy(() => import('./pages/Telephony'))
const Settings = lazy(() => import('./pages/Settings'))
const Developers = lazy(() => import('./pages/Developers'))
const ApiReference = lazy(() => import('./pages/ApiReference'))
// Lazy like every other route: the billing page pulls in the plan grid, the
// invoice view and (on demand) Razorpay's checkout.js — none of which belong
// in the entry bundle for a user who never opens it.
const Billing = lazy(() => import('./pages/Billing'))
const Receipt = lazy(() => import('./pages/Receipt'))
const WhatsApp = lazy(() => import('./pages/integrations/WhatsApp'))
const AgentsList = lazy(() => import('./pages/agents/AgentsList'))
const CreateAgent = lazy(() => import('./pages/agents/CreateAgent'))
const EditAgent = lazy(() => import('./pages/agents/EditAgent'))

/** Shown while a route chunk downloads. Matches the page-header + card rhythm
 *  every panel opens with, so the shell does not visibly jump on arrival. */
function RouteFallback() {
  return (
    <div className="space-y-6" role="status" aria-label="Loading page">
      <div className="space-y-2">
        <div className="skeleton h-7 w-52" />
        <div className="skeleton h-4 w-80 max-w-full" />
      </div>
      <div className="skeleton h-64 w-full rounded-2xl" />
    </div>
  )
}

export default function App() {
  return (
    <AppShell>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          {/* Homepage: Overview dashboard (setup path + daily pulse). The
              conversational Copilot moved to /copilot (2026-07-22 IA change). */}
          <Route path="/" element={<Overview />} />
          <Route path="/copilot" element={<CopilotChat />} />
          <Route path="/live" element={<Live />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/recordings" element={<Recordings />} />
          <Route path="/transcripts" element={<Transcripts />} />
          {/* Intelligence layer — automatic post-call pipeline */}
          <Route path="/intelligence" element={<Intelligence />} />
          <Route path="/intelligence/:callKey" element={<IntelligenceCall />} />
          <Route path="/followups" element={<FollowUps />} />
          {/* Automations panel removed 2026-07-21 (too complex for now); the
              background rules engine still runs — notifications keep working */}
          <Route path="/automations" element={<Navigate to="/intelligence" replace />} />
          <Route path="/reports" element={<Reports />} />
          {/* Manual AI Analysis is retired: analysis is automatic now */}
          <Route path="/ai-analysis" element={<Navigate to="/intelligence" replace />} />
          <Route path="/conv-analytics" element={<ConvAnalytics />} />
          {/* Integrations — channels that act on what a call produced. Only
              WhatsApp today; /integrations lands on it until there is a hub. */}
          <Route path="/integrations" element={<Navigate to="/integrations/whatsapp" replace />} />
          <Route path="/integrations/whatsapp" element={<WhatsApp />} />
          <Route path="/outbound" element={<Outbound />} />
          <Route path="/contacts" element={<Contacts />} />
          {/* Customer Intelligence Hub — per-contact relationship workspace */}
          <Route path="/contacts/:id" element={<ContactHub />} />
          {/* BYON — trunks + DID→agent routing (multi-tenant telephony) */}
          <Route path="/telephony" element={<Telephony />} />
          {/* Account settings — profile details (test-call destination phone) */}
          <Route path="/settings" element={<Settings />} />
          {/* The integration surface is a destination, not a Settings tab:
              /developers is where keys are minted, /developers/reference is the
              printable manual. The old /api-docs URL is kept as a redirect —
              it may already be in someone's bookmarks or an email. */}
          <Route path="/developers" element={<Developers />} />
          <Route path="/developers/reference" element={<ApiReference />} />
          <Route path="/api-docs" element={<Navigate to="/developers/reference" replace />} />
          {/* Plan, minutes, invoices (deploy/PLAN-razorpay-subscriptions.md) */}
          <Route path="/billing" element={<Billing />} />
          <Route path="/billing/receipt/:paymentId" element={<Receipt />} />
          {/* Build — one Agents panel: list + New Agent. Each card chats with
              its agent through the live engine (the Intent tester it replaced
              was removed 2026-08-01). */}
          <Route path="/build/agents" element={<AgentsList />} />
          <Route path="/build/agents/new" element={<CreateAgent />} />
          <Route path="/build/agents/:name" element={<EditAgent />} />
          {/* Intent tester is gone; keep the redirect so old links/bookmarks
              land on the Agents panel instead of a 404. */}
          <Route path="/build/intent" element={<Navigate to="/build/agents" replace />} />
          {/* Old read-only Agent Settings is superseded by the editable builder */}
          <Route path="/agent-settings" element={<Navigate to="/build/agents" replace />} />
          {/* Shared agent link — see ShareLanding above */}
          <Route path="/s/:token" element={<ShareLanding />} />
          <Route path="*" element={<Placeholder title="Not Found" sub="That route does not exist." />} />
        </Routes>
      </Suspense>
    </AppShell>
  )
}
