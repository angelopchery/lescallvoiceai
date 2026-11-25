# Conversation State Management Layer

> **Status (2026-10-18):** the mechanism described here is current, but the LLM names are historical: extraction and generation now run on Gemini via `LLM_BASE_URL` / `LLM_MODEL` (RUNBOOK §43), tool-mode values pass `_drop_non_values` (RUNBOOK §50), and slots gained `ask_gated`, `merge_policy`, `max_asks` and retirement via `retire_slots` (RUNBOOK §56, §61, §75). Current agent model: `docs/PLATFORM.md` §5.

**Module:** `app/voice/state_manager.py` · **Integrated into:** `app/voice/conversation.py`, `conversation_call_bridge.py`, `app/agent_definition/schema.py` · **Tests:** `tests/test_state_manager.py` (28 cases)

## The problem it solves

Callers front-load information: *"I want a 3BHK apartment in Vapi under 50 lakhs."*
The scripted flow then asked *which city? / what configuration? / what budget?* — all already answered. This layer gives the agent a persistent, structured, audited memory so it **acknowledges what it knows and asks only for what's missing**.

## Architecture

```
                       user utterance (per turn, incl. shortcut turns)
                                      │
                     ┌────────────────▼─────────────────┐
                     │ ConversationStateManager          │
                     │  observe_user()                   │
                     │   ├─ extract_entities()  ~0.1 ms  │  pure regex + KB match
                     │   └─ merge_state(src="regex")     │  null-protected, audited
                     └───────┬───────────────┬──────────┘
             (parallel, off  │               │  (same turn, sync)
              the hot path)  │               │
        ┌────────────────────▼──┐   ┌────────▼─────────────────────────┐
        │ _llm_extract_slots()  │   │ build_prompt_context()           │
        │ Groq JSON extraction, │   │  ━━ CONVERSATION STATE ━━        │
        │ 2.5 s timeout,        │   │  ✓ filled  ✗ missing  ? pending  │
        │ merge fill_only=True  │   │  MISSING (priority order)        │
        │ (race-safe: can only  │   │  SUMMARY (template, no LLM)      │
        │  fill empty slots)    │   │  RULES + ONE next question       │
        └───────────────────────┘   └────────┬─────────────────────────┘
                                             │ injected into every system prompt
                                    ┌────────▼─────────┐
                                    │  LLM (Groq)      │→ response speaks to state,
                                    └──────────────────┘  never re-asks ✓ fields
```

**Turn algorithm** (spec steps 1-7): transcript → `extract_entities` → `merge_state` → summary (recomputed from state) → `detect_missing_slots` → prompt built with state block → LLM asks only the single highest-priority missing field. All synchronous parts are string/regex work — the layer adds **< 0.5 ms** per turn (asserted by tests).

## Data models

```python
SlotSpec(name, priority, required, label, question{lang→str}, summary_known, summary_unknown)
AuditEntry(field, old, new, source: regex|llm|legacy|system, turn, timestamp, utterance)
state: dict[slot→value|None]        # e.g. {"location": "Vapi", "bhk": "3BHK", ...}
```

Slots are **per-agent**, defined in agent YAML (`slots:` list — see `agents/pramukh/agent.yaml`) or derived automatically: `bhk` in `qa_field_signals` → real-estate set; booking stages (`INVITE_VISIT`/`SLOT_BOOKING`/`CONFIRMATION`) → name+meeting-time; otherwise name-only (pitch agents).

## Merge semantics (conflict resolution)

- **Null-protection:** extracted `None`/empty values are ignored — real data is never erased.
- **Latest-wins:** *"Actually make it a 2BHK"* overwrites `3BHK` and appends `AuditEntry(old=3BHK, new=2BHK, source=regex, turn=7)`.
- **`fill_only=True`** for the async LLM extractor: a slow/stale completion can only fill still-empty slots — it can never clobber newer info. This makes the parallel design race-safe *by construction* (no locks needed).
- Negation-aware: *"3 bhk nahi, 2 bhk chahiye"* → `2BHK` (negated candidates excluded, last mention wins).

## Extraction coverage (telephony-tuned)

Hinglish + Devanagari + Gujarati: BHK (digits, word-numbers *teen/तीन*, `३`), budget (lakh/crore/लाख/करोड़, word-numbers *pachas*, ranges `40-50`), locations (KB canonical + script aliases, last-mention-wins), property type, timeline (*next month/agle mahine/Diwali/just exploring*), loan (yes/no incl. *cash/bina loan*), purpose (investment vs self-use), occupation. Whatever the regexes miss on messy phrasing, the parallel Groq JSON extractor back-fills a turn later (`source=llm`).

## Prompt template (injected every LLM call)

```
━━ CONVERSATION STATE (ground truth — overrides the stage script) ━━
  ✓ Name: Rahul
  ✓ Location: Vapi
  ✓ Configuration: 3BHK
  ✓ Budget: 50 Lakhs
  ✗ Timeline: Unknown
  ✗ Loan Required: Unknown
MISSING (priority order): Timeline, Loan Required
SUMMARY: Customer wants a 3BHK apartment in Vapi under 50 Lakhs. Timeline not yet discussed. Loan requirement not yet discussed.
RULES:
  • NEVER ask about any ✓ field — the caller already told you. Acknowledge ✓ facts naturally when relevant.
  • Ask ONLY ONE question next, about: Timeline. Suggested phrasing: आप कब तक purchase करने का सोच रहे हैं?
```

This single block **replaced** the three previously-overlapping prompt sections (`Known so far` / `CONFIRMED FIELDS` / `ALREADY COLLECTED`) — the prompt got *shorter* and self-consistent. When `missing=[]` the rules flip to *"All required information is collected — move to confirming and closing."* which drives the stage machine forward (slot-aware early advance out of DISCOVERY).

## Voice-agent edge cases handled

| Case | Mechanism |
|---|---|
| Multi-intent turn ("agle mahine lena hai aur loan chahiye") | all extractors run per utterance |
| Fragmented info across turns | merge preserves prior slots |
| Corrections ("actually 2BHK") | latest-wins + audit trail |
| Noisy ASR fragments | extract what parses, ignore the rest; never throws (hot path is fully exception-guarded) |
| Interruptions/barge-in | extraction is synchronous per transcript — an interrupted turn's slots still land; extractor task cancelled per new turn |
| Slot answer inside a shortcut turn ("thanks, 2BHK works") | `observe_user` runs **before** shortcut routing |
| Name confirmation flow | `? Name: awaiting confirmation` (pending fields excluded from `missing` so the LLM doesn't double-ask) |
| Phone numbers | kept out of budget/BHK regexes (unit words required); phone stays in the legacy masked path, never echoed |

## Persistence

At call teardown (`TelephonySession.stop`) the in-flight extractor is cancelled first, then the full snapshot — state, filled/missing, summary, audit log — is written as JSON next to the call recordings (fallback: `recordings/_state/state_<conn_id>.json`).

## Example conversation (real transcript, Groq llama-3.3-70b, pramukh agent)

```
USER : Main Rahul bol raha hoon, mujhe Vapi mein 3BHK apartment chahiye, budget 50 lakh tak
AGENT: Aapka naam Rahul hai, right? Vapi mein 3BHK apartment ke liye aapka budget 50 lakh
       tak hai, yeh samajh mein aaya. Aap kab tak purchase karne ka soch rahe hain?
                                   → filled: name, location, bhk, property_type, budget
USER : Actually 3 nahi, 2BHK kar do
AGENT: So, 2BHK in Vapi, that's a good choice, and your budget is still 50 lakh, got it.
       When are you planning to purchase, Rahul?
                                   → audit: bhk 3BHK→2BHK
USER : Agle mahine tak lena hai aur home loan bhi chahiye hoga
AGENT: Theek hai... Humare paas Vapi mein ek accha option hai, Aura project, jo aapke
       budget mein aata hai — would you like to see it in person?
                                   → missing=[] → auto-pivot to site visit
```

## Scalability considerations

- **Per-call isolation:** one manager per `ConversationRouter` per call — no shared mutable state, no cross-call contention; N concurrent calls scale linearly.
- **O(1) hot path:** precompiled module-level regexes (compiled once per process); extraction is O(len(utterance)); prompt/summary are pure string templates.
- **LLM extractor budget:** ≤1 in-flight task per call (new turn cancels the previous), 120-token JSON completions on Groq — negligible cost/latency, and disable-able via `ENABLE_LLM_SLOT_EXTRACTOR=false`.
- **Slot vocabulary is config, not code:** new agent types ship as YAML `slots:` lists — no code changes; unknown slots in payloads are ignored defensively.
- **Horizontal:** state lives in the call process and is snapshotted to JSON at teardown; moving to Redis/DB later only requires swapping the persistence sink, not the manager.
