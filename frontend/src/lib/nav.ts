import {
  Radio, ListChecks, Mic2,
  MessagesSquare, BrainCircuit, Microscope, PhoneOutgoing,
  Boxes, CalendarClock, FileText, LayoutDashboard,
  MessageCircle, PhoneForwarded, Settings as SettingsIcon, BookUser,
  CreditCard, Braces, MessageSquareShare,
} from 'lucide-react'

// Every user sees the same navigation — roles are flattened; there is no
// admin tier and no admin-only pages.
//
// Group order follows the tenant journey: set up (Build → Numbers →
// Campaigns), then operate (Monitoring), then learn (Intelligence).
/** `badge` names the counter an item shows: 'live' = calls in progress (the
 *  urgent pulsing pill), 'wa' = WhatsApp messages waiting for approval. */
export type NavItem = { to: string; label: string; icon: any; badge?: 'live' | 'wa' }
export type NavGroup = { title: string; items: NavItem[] }

export const NAV: NavGroup[] = [
  {
    title: 'Home',
    items: [
      { to: '/', label: 'Overview', icon: LayoutDashboard },
      { to: '/copilot', label: 'Copilot', icon: MessageCircle },
    ],
  },
  {
    // One panel for everything agent-related: the list, the "New Agent"
    // button, and the intent tester (a tab) all live under /build/agents.
    title: 'Build',
    items: [
      { to: '/build/agents', label: 'Agents', icon: Boxes },
      { to: '/telephony', label: 'Numbers (BYON)', icon: PhoneForwarded },
    ],
  },
  {
    title: 'Campaigns',
    items: [
      { to: '/contacts', label: 'Contacts', icon: BookUser },
      { to: '/outbound', label: 'Outbound Calls', icon: PhoneOutgoing },
    ],
  },
  {
    title: 'Monitoring',
    items: [
      { to: '/live', label: 'Live Calls', icon: Radio, badge: 'live' },
      { to: '/activity', label: 'Call Activity', icon: ListChecks },
      { to: '/recordings', label: 'Recordings', icon: Mic2 },
      { to: '/transcripts', label: 'Call Transcripts', icon: MessagesSquare },
    ],
  },
  {
    // Post-call intelligence is automatic (deploy/PLAN-intelligence-layer.md):
    // every completed call is analyzed by the pipeline — there is no manual
    // "analyze" step anymore.
    title: 'Intelligence',
    items: [
      { to: '/intelligence', label: 'Intelligence', icon: BrainCircuit },
      { to: '/followups', label: 'Follow-ups', icon: CalendarClock },
      { to: '/reports', label: 'Reports', icon: FileText },
      { to: '/conv-analytics', label: 'Conversation Analytics', icon: Microscope },
    ],
  },
  {
    // Channels that act on what a call produced. WhatsApp first: a caller who
    // agreed on the call gets the brochure / form the agent promised.
    title: 'Integrations',
    items: [
      { to: '/integrations/whatsapp', label: 'WhatsApp', icon: MessageSquareShare, badge: 'wa' },
    ],
  },
  {
    // The integration surface is its own destination, not a tab inside
    // Settings. It is where a customer's engineer lives while they wire up a
    // CRM, and burying the reference two clicks deep inside an account page
    // made it read as an afterthought.
    title: 'Account',
    items: [
      { to: '/developers', label: 'Developers', icon: Braces },
      { to: '/billing', label: 'Billing', icon: CreditCard },
      { to: '/settings', label: 'Settings', icon: SettingsIcon },
    ],
  },
]
