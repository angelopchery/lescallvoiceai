# LesCall Frontend — World-Class SaaS Redesign Plan

**Status: IMPLEMENTED & DEPLOYED — 2026-07-22.**
Approved scope was R0–R6 including full R5 (IA restructure), the glass brand kept with
shadcn restyled to match, and contact-sharing UI folded into R2. All of it is built,
typechecks clean, and is serving from the `lescall-dashboard` container.
Prepared 2026-07-22 from a 4-agent codebase audit (product/IA map, operational-pages audit,
insight-pages audit, shared design-layer audit).

### What shipped

| Ref | Scope | State |
| --- | --- | --- |
| R0 | Foundation — type/radius/colour tokens, `lib/hooks.ts`, `lib/chartColors.ts`, `components/confirm.tsx` (`useConfirm`/`usePrompt`), `IconButton`/`LiveDot`, 21 shadcn primitives, dead pages deleted (`AgentSettings`, `AiAnalysis`) | done |
| R5 | IA — `Overview` is the new home, Copilot moved to `/copilot`, nav regrouped (Home · Build · Campaigns · Monitoring · Intelligence · Account), breadcrumbs on detail pages, real notification centre in the topbar | done |
| R4 | Operate family — Telephony, Outbound, Live, Activity, Recordings, Transcripts | done |
| R1+R2 | Insight family — Intelligence, IntelligenceCall, ContactHub (incl. sharing/grants UI), Contacts, ConvAnalytics, FollowUps, Reports, Settings, `components/insight.tsx`, `components/analyticsCharts.tsx` | done |
| R1+R3 | Build family — CopilotChat, AgentsList, CreateAgent, EditAgent, AgentFormSections, IntentTester | done |
| R6 | Polish — every native `confirm`/`prompt` replaced, hard-px type mapped onto the scale (display tier `text-4xl`/`text-5xl` added for metrics), layout-matched skeleton variants on `AsyncState`, aria-hidden backdrops, named icon buttons, per-bucket empty states, route-level code splitting | done |

**Measured result:** entry bundle 740 kB → 385 kB (216 → 123 kB gzipped); every panel
now loads as its own chunk on first navigation. `tsc --noEmit` and `vite build` both green.

---

## 1 · Product understanding (Phase 1)

LesCall is a multi-tenant SaaS for AI voice-phone agents. The value loop:
**build an agent** (Copilot chat at `/` or 9-step wizard) → **connect a number** (BYON, `/telephony`)
→ **go live** (bind agent to number direction) → **calls happen** (inbound + `/outbound` campaigns)
→ **watch & review** (`/live`, recordings, transcripts) → **intelligence** (auto post-call pipeline:
scores, commitments, follow-ups, per-contact memory/timeline) → **improve the agent**.

**Personas** (roles are deliberately flat — no admin tier):
1. *Builder/owner-operator* — heaviest in week 1 (Copilot, Agents, Telephony, Settings).
2. *Campaign runner* — daily in Contacts + Outbound + Live.
3. *Ops monitor* — Live, Activity, Recordings, health pill.
4. *Sales manager/analyst* — the "return every day" persona: Intelligence, Follow-ups, ContactHub, Reports.

**Structural findings:**
- **Nav order fights the setup journey**: Monitoring/Intelligence rank above Build/Telephony/Campaigns,
  so a new tenant's most prominent panels are empty ones.
- **Four+ overlapping call-review pages** (Activity, Recordings, Transcripts, Intelligence/IntelligenceCall)
  with **three separate transcript renderers** and zero cross-links between views of the same call.
- **Two contact pages over one dataset** (`Contacts` phonebook vs `Outbound` disposition view, same API).
- **Major workspaces are orphan routes** — `/intelligence/:callKey` and `/contacts/:id` reachable only via
  row clicks; no breadcrumbs.
- **No onboarding spine**: guidance lives in scattered (good) empty states; nothing tracks
  "agent created / number connected / first call" and there is no Overview home despite `/api/overview` existing.
- **Built-but-unsurfaced backend**: notification center (`/api/intel/notifications` — bell currently only
  shows service health), contact sharing grants, intel job queue, in-chat test call (`/api/copilot/try`).
- **Dead code**: `pages/AgentSettings.tsx` (unrouted), stale scaffolding copy in `Placeholder.tsx`,
  4 legacy redirect routes.

## 2 · Design audit (Phase 2) — merged top findings

**A. Interaction-pattern fragmentation (highest severity)**
1. **Destructive-confirm chaos**: 13 native `window.confirm` + 1 `prompt()` (EditAgent ×7, Outbound ×3,
   Contacts ×2, Reports ×1) coexisting with inline-relabel confirms and real `Modal` confirms — 4–5 idioms.
2. **Two hand-rolled modals bypass the focus-trapped `Modal`**: `EditAgent` ExtractReview (no focus trap,
   no scroll-lock) and CopilotChat's mobile sheet — genuine keyboard traps.
3. **Three tab systems** (EditAgent `role=tab` without keyboard support, CreateAgent step rail, `Segmented`);
   **two filter idioms** (Segmented vs hand-rolled chips) for the same job on sibling pages.
4. **Hand-rolled popover idiom ×4** (Topbar bell/help/search, HealthPill): `fixed inset-0` click-catcher, no aria.
5. **Icon-only actions with `title` but no `aria-label`** (FollowUps, Reports); destructive Cancel adjacent to
   Complete with identical styling and no confirm/undo; Live nests a `<Button>` inside a `role="button"` div.

**B. Component duplication**
6. `SentenceBarList` = near-verbatim clone of kit `BarList`; three color-swatch renderers in one file (ConvAnalytics).
7. Score encoding: same `lead_score` rendered 3 ways (ScorePill / plain number / RadialRing) with 2 color semantics.
8. Duplicated composites between IntelligenceCall ↔ ContactHub: ring cluster, Chips (×4 inline), commitment
   cards, timeline markup. Live-pulse dot reinvented ×3. Icon-button pattern reinvented ×6 (Telephony).
9. Raw inputs duplicating kit `Input` with wrong radius (EditAgent Documents ×3); `<a>` styled as Button with
   mismatched radius (Recordings); repeated badge class strings (AgentsList ×3, CopilotChat chips ×3 groups).
10. CopilotChat (the homepage) re-declares a local `EmptyState` and hand-rolls most of its controls.

**C. Token/scale drift**
11. **17 distinct arbitrary font sizes** (incl. half-pixels `13.5/12.5/11.5/10.5` and a `text-[9px]`);
    detail-page H1 = 18px vs list H1 = 20px.
12. **21 hardcoded hex colors** in charts, including a real collision: *Negative* and *Hot* are the identical
    `#fb7185`; greens `#13f4a3` diverge from `--accent`. Risk rings hardcode `#fb7185`/`#817fff`.
13. Radius drift: buttons `rounded-lg`(12px) vs shadcn `rounded-md`(10px); `rounded-xl` copies on some controls.
14. Two token systems now coexist and are fully disjoint (app vars vs shadcn vars — 0 shared references).

**D. State/UX gaps**
15. **No URL persistence** of any filter/search/page (Intelligence, Contacts, FollowUps, Reports) — refresh loses all.
16. **No debounce** on server-backed search in Intelligence + Contacts (request per keystroke) while 4 other
    pages debounce; no shared `useDebounced`.
17. Loading inconsistency: skeletons (most) vs bare Spinner (EditAgent tabs, ContactHub timeline) vs nothing
    (ContactHub calls list — and its error is silently swallowed); skeletons don't match final layout (shift).
18. Double empty states rendered simultaneously (Intelligence, FollowUps).
19. Mutations without undo (Outbound disposition PATCH on select-change).
20. Mobile: AgentsList card action row wraps badly at 375px; tap targets mixed 34/36px.

**E. Accessibility (systemic)**
- Contacts groups sidebar: `div role="button"` with **no tabIndex/keyboard** (worst single a11y bug).
- Disclosure toggles missing `aria-expanded` (Telephony ×3). 191 native `title=` tooltips (invisible to touch,
  unreliable for SR). Global focus = indigo outline vs shadcn ring — two focus languages. 9–11px dim text on
  translucent surfaces flirts with contrast floors.

## 3 · Design system (Phase 3)

Direction: **keep the brand** (obsidian + glass + emerald/indigo — it's distinctive and already polished) and
**industrialize it**: one type scale, one radius law, tokenized charts, one focus language, shadcn as the
interaction layer under the existing skin. Reference bar: Linear/Stripe/Supabase density and calm.

**3.1 Typography scale** (Tailwind `fontSize` overrides → arbitrary sizes get purged; shadcn's `text-sm/xs`
automatically lands on our scale):
| Token | Size/leading | Replaces / use |
|---|---|---|
| `text-2xs` (new) | 11px/16 · +0.05em when uppercase | eyebrows, TH, badges (kills 10/10.5/11px arbitraries; 9px banned) |
| `text-xs` | 12px/18 | secondary text, table meta (kills `text-[12px]`×106, 11.5px) |
| `text-sm` | 13px/20 | **body default** (kills 13/13.5/12.5px; shadcn components inherit it) |
| `text-base` | 14px/22 | emphasized body, section h4 |
| `text-lg` | 16px/24 | card titles |
| `text-xl` | 18px/26 | detail-page H1 |
| `text-2xl` | 20px/28 | list-page H1 (PageHeader) |
| `text-3xl` | 24px/32 | hero/metric |
Fonts unchanged (Inter Variable body, Plus Jakarta display). Metric numerals keep `.metric` (tabular-nums).

**3.2 Spacing**: 4px grid. Card padding `p-5` standard (`p-4` dense, `p-6` hero only — kills the p-4/5/6/8 drift).
Section gap `gap-6`; intra-card stack `space-y-4`; page gutter `px-4 sm:px-6`; content `max-w-[1440px] mx-auto`.

**3.3 Radius law** (`--radius: 12px` already set): controls (buttons/inputs/selects) `rounded-md` 10px ·
chips/badges `rounded-full` · cards `rounded-2xl` 14px · modals/popovers `rounded-xl` 12px · sidebar shell 18px.
Fix: kit Button & Recordings' link-button move from 12px→10px to match shadcn.

**3.4 Color tokens**:
- Keep app vars (`--bg --surface --border --text* --accent --accent-2`) as the *surface/brand* layer.
- shadcn vars (`--primary --muted --ui-border --ui-accent …`) are the *component* layer (already installed,
  dark values tuned to obsidian). Rule: pages use semantic Tailwind tones (mint/indigo/amber/rose) only via
  primitives (Badge/StatusBadge/StatCard), never inline hex.
- **Charts**: replace all 21 hex literals with the installed `--chart-1..5` tokens + a semantic map:
  positive/mint `--chart-2`-family, negative `rose`, warm/hot `amber` (fixes the Negative=Hot collision),
  cold `indigo`, neutral `--text-dim`. One `chartColors` module exports both.
- Dark-mode `--ui-border/--input` carry baked-in alpha → **rule: never use opacity modifiers on
  `border-border`/`bg-input`** (documented; lint-greppable).
- **Focus unification**: set `--ring` to the indigo accent (light `243 75% 59%`, dark `246 100% 76%`) so shadcn
  rings and the global `:focus-visible` outline speak one color.

**3.5 Elevation**: 3 levels — `shadow-glass` (resting card), `shadow-glass-lg` (popover/modal),
`glow-mint|indigo` (highlight only, ≤1 per view). Backdrop `bg-zinc-950/40`.
**z-ladder formalized**: topbar 20 · popover 40 · drawer 50 · dialog 80/90 · toast 100 (Radix portals pinned to it).

**3.6 Animation**: `fade-up` for page/section entry (already), `tailwindcss-animate` for Radix
open/close (150–200ms), hovers 150ms, no motion >300ms; global reduced-motion rule already handles opt-out.

**3.7 Layout rules**: every page = `PageHeader` (+ breadcrumb slot for detail pages) → optional KPI band →
content. Detail workspaces standardize on `xl:grid-cols-3` (2/3 + 1/3 rail). Tables always inside kit `Table`.
Skeletons must mirror final layout (per-page skeleton like ConvAnalytics's, generalized).

## 4 · shadcn mapping (Phase 4)

Registry verified: all needed items exist in v3-compatible `new-york` style (fetch via `npx shadcn@2 add …`).
Strategy: **swap engines under existing APIs** — kit/ui components keep their names & props, internals become
shadcn primitives restyled to the glass aesthetic. Pages migrate with near-zero churn; hand-rolled one-offs die.

| Current | Replacement | Reason |
|---|---|---|
| `ui.Button` (129 uses) + 6 hand-rolled icon-buttons + Recordings `<a>`-button | **button** (cva) + new `IconButton` wrapper | one variant system (primary/ghost/soft/danger→default/outline/secondary/destructive), consistent radius/tap targets, asChild for link-buttons |
| kit `Input/Textarea/Select/Field` + 3 raw EditAgent inputs + Settings icon-inputs | **input, textarea, select (Radix), label, field**; `SearchInput` gains `icon/onEnter/onClear` | kills all hand-rolled variants; Radix select = keyboard + mobile-safe |
| `ui.Badge` + `StatusBadge` + ~10 hand-rolled pills + chip groups | **badge** (cva) + new `Chips` | STATUS_TONE map becomes variant resolver; one pill anatomy |
| kit `Table` family | **table** under same API | already consolidated (12/12 tables) — lowest-risk swap |
| `Modal/FullscreenModal/SlideOver` + 2 rogue overlays | **dialog, sheet** | Radix focus/scroll/portal management; rogue overlays deleted |
| 13× `window.confirm` + 1 `prompt()` | **alert-dialog** via new `useConfirm()` hook + small input-dialog | one styled, focus-managed confirm idiom app-wide |
| Topbar quick-jump (hand-rolled) | **command (⌘K)** + kbd | real palette: navigate + search contacts/calls/agents; discoverable shortcuts |
| Topbar bell/help, HealthPill popovers | **popover / dropdown-menu**; bell becomes real **notification center** backed by existing `/api/intel/notifications` | kills `fixed inset-0` idiom; surfaces built-but-hidden backend |
| EditAgent tabs, AgentsList Segmented-as-tabs | **tabs** | keyboard-accessible, aria-complete; `Segmented` stays for filters |
| `.skeleton`/`SkeletonBlock`, bare Spinners | **skeleton** + per-page layout skeletons via `AsyncState` | no layout shift, one loading language |
| 191 `title=` attrs on icon actions | **tooltip** | accessible names + touch-visible |
| `Toast.tsx` | **sonner** (phase-late) | promise toasts + **undo action** (needed for disposition changes); existing toast is fine until then |
| Contacts groups sidebar, Sidebar nav internals | **button/asChild patterns** (full **sidebar** block optional, later) | fixes keyboard-inoperable nav; avoid risky shell rewrite now |
| ConvAnalytics charts | keep custom SVG + tokenized colors (**chart** wrapper optional) | charts are light & fast; color tokens are the actual fix |
| — (new) | **empty** patterns folded into `EmptyState`; **breadcrumb** for detail pages; **avatar** (Topbar/ContactHub) | completes the set |

**New shared composites** (built on the above, killing the duplication ledger): `ScoreCell`/`ScoreRing`
(one threshold+color law for lead/opp/risk), `Chips`, `FilterChips`, `CommitmentCard`, `TimelineList`,
`LiveDot`, `IconButton`, `StatTile` (ContactHub lifetime), `useConfirm`, `useDebounced` (shared),
`useUrlState` (filters/page/search → URL params).

## 5 · Implementation plan (Phase 5) — per page

Each entry: current issues → improvements → components. All additive; SPA rebuild at each deploy checkpoint.

**R0 — Foundation (no visual change intended)**
Type-scale tokens in tailwind config; radius law; `--ring` indigo; chartColors module; `cn` adoption rule;
add shadcn primitives (button, input, select, textarea, label, badge, table, dialog, sheet, alert-dialog,
tabs, tooltip, popover, dropdown-menu, command, skeleton, separator, avatar, breadcrumb) restyled to glass;
re-skin kit/ui internals onto them keeping APIs; `useConfirm/useDebounced/useUrlState`; delete dead
`AgentSettings.tsx`; fix `Placeholder` copy → real 404 with CTA. *Gate: visual-diff smoke on 4 key pages, build green.*

**R1 — Trust & safety sweep (app-wide, mechanical)**
Replace all native confirm/prompt with `useConfirm`; port 2 rogue modals to dialog/sheet; tooltips+aria-labels
on every icon action; `aria-expanded` on disclosures; fix Contacts sidebar keyboard nav; fix Live nested-button;
tap-target normalization; debounce Intelligence+Contacts search; URL state on Intelligence/Contacts/FollowUps/Reports.

**R2 — Insight family** (the daily-return surfaces)
- *Intelligence*: merge double empty state; NL search via `SearchInput`; `ScoreCell` for L/O/R columns;
  shadcn checkbox; column tuning so Summary is readable ≤1200px; result counts.
- *IntelligenceCall*: breadcrumb; H1 to scale; shared `ScoreRing` cluster, `Chips`, `CommitmentCard`,
  `TimelineList`; transcript search match-count + clear; prioritized intelligence sections.
- *ContactHub*: same composites; fix three-way loading inconsistency + surface calls error; `FilterChips`;
  StatTile lifetime; grants/sharing UI **only if approved** (backend exists).
- *FollowUps*: labeled actions + confirm on Cancel; undo via sonner (or confirm until sonner lands).
- *ConvAnalytics*: delete `SentenceBarList` (BarList `wrap` prop); one Legend; tokenized colors (fixes
  Negative=Hot); drop triple-redundant trends rendering to sparkline-cards + table; add days filter to match Intelligence.
- *Reports*: per-row loading state; non-done rows not clickable; empty-state cross-link.

**R3 — Build family**
- *CopilotChat*: shared EmptyState/Button/Badge; mobile sheet → `Sheet`; add **Test-call / open-agent actions**
  on agent cards in chat (uses existing `/api/copilot/try`); brand header restored.
- *AgentsList*: Badge/Chips adoption; one confirm idiom; mobile action-row regrouped (overflow `dropdown-menu`);
  `LiveDot`.
- *CreateAgent*: keyboard-reachable Next (kill span-wrapper); step rail as tabs-style nav.
- *EditAgent*: shadcn **tabs**; Documents tab on kit `Input`; ExtractReview → `Dialog`; 7 confirms + prompt →
  `useConfirm` + input dialog; per-tab skeletons.
- *IntentTester*: no changes needed (cleanest page).

**R4 — Operate family**
- *Telephony*: `IconButton` for copy buttons ×6; disclosures aria; non-copyable `<code>` de-styled;
  inline link to Settings phone with status.
- *Outbound/Contacts*: shared import/add flow components; disposition undo; row-nav vs row-actions
  clarified (chevron affordance); client-side pagination honesty (server paging later).
- *Live/Activity/Recordings/Transcripts*: `LiveDot`; Activity onto `AsyncState`; Recordings link-buttons;
  9px→scale; **cross-links between the four views of the same call** (call id → Intelligence workspace).
- *Settings*: `InputWithIcon`; setup-status card (agent? number? test phone?) linking the journey.

**R5 — IA & navigation (approval-gated separately, see Decisions)**
Nav reorder to match the journey (Build → Numbers → Campaigns → Monitor → Intelligence → Account);
Transcripts moves to Monitoring; breadcrumbs everywhere; **Overview home** at `/` (setup checklist from
`/api/overview`, today's KPIs, hot leads, live now) with Copilot moved to `/copilot` (or kept at `/` — decision);
optional later: merge Activity/Recordings/Transcripts into one Calls workspace with tabs.

**R6 — Final polish (Phase 7 of the brief)**
Per-page layout skeletons; empty states with next-step CTAs everywhere; hover/active/disabled sweep;
keyboard walkthrough of every flow; contrast pass on dim text; 375px sweep; reduced-motion QA;
`rtk gain`-style perf sanity (bundle size, lazy routes); deploy + smoke.

**Testing/rollout**: after each R-phase — `npm run build`, visual smoke of affected pages, deploy via
`docker compose -f deploy/docker-compose.services.yml up -d --build spa`, hard-refresh check. Existing test
suite unaffected (backend untouched except nothing; notification-center wiring is read-only API consumption).

**Expected outcome**: one interaction language (confirms, tabs, tooltips, focus), one type/radius/color system,
~40% less bespoke UI code in pages, accessible keyboard paths on every flow, URL-stable filters, an onboarding
spine, and the hidden intelligence features (notifications, sharing, in-chat test call) finally surfaced —
while keeping the distinctive obsidian-glass-emerald brand.

## 6 · Decisions needed before implementation

1. **Approve overall plan?** (R0–R4 + R6 are the core; each phase independently shippable.)
2. **IA changes (R5)** — nav reorder + Overview home + Copilot to `/copilot`: yes / keep current IA / Overview
   but Copilot stays home.
3. **Aesthetic**: keep obsidian-glass-emerald (recommended, plan assumes it) vs move to flat opaque
   Linear/Stripe look (bigger change, restyles 74 cards).
4. **Contact sharing UI** (grants API exists; previously gated): include in R2 or keep hidden.
