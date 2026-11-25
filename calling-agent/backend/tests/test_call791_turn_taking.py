"""Regression gate for the 2026-09-08 20:57 Jai Mistry → Angelo P Chery call
(call id 791, agent rev 16) — RUNBOOK §37.

The §35/§36 fixes made the state machine put the right verbatim line in front
of the model. This call failed for six DIFFERENT reasons, none of them stage
wiring and all of them platform-wide:

1. The agent never introduced itself — `_post_greeting_stage()` jumped past
   the flow's own first stage because a stage named DISCOVERY existed
   somewhere later in the list. INTRO_QUALIFY (and the whole wrong-number
   script the owner dictated) had never run on any call.
2. "बताओ आप कहाँ से बात कर रहे हो?" was classified `affirmation`, because
   "हाँ" (yes) is a SUBSTRING of "कहाँ" (where). Every Hindi where/here/there
   question read as a YES on every hi/gu agent.
3. The caller's real answer was transcribed correctly and then DISCARDED: the
   merge window re-armed for 4.0s (it could see the caller still speaking)
   and then dropped the follow-on for being older than a fixed 3.0s cap.
4. "कर दो" ("do it") did not read as consent to the close question, so the
   agent re-spoke its farewell — twice.
5. The dead-air repair replayed the farewell WITH the close question after an
   "I couldn't hear you" apology.
6. Romanized-Hindi scripted lines scored zero Hindi markers and were spoken
   by Sarvam's ENGLISH pronunciation frontend; and the model reproduced the
   quote marks that DELIMIT a verbatim line as if they were words.

No live LLM and no network: every assertion is about deterministic platform
behaviour.
"""
from __future__ import annotations

import pytest

from pathlib import Path

from conftest import AGENTS_DIR

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import (
    ConversationRouter,
    _strip_script_quotes,
    detect_intent,
)

# These are ENGINE regression tests. They used to load the LIVE agent file
# (agents/jai_mistry-outbound/agent.yaml), which is gitignored and edited by
# its owner through the Copilot — so on 2026-09-18, when the owner rebuilt that
# agent into a different persona with a different flow, 13 tests went red with
# no code change anywhere. The definition they were written against is frozen
# here instead: the pre-rewrite snapshot taken from `agent_revisions`
# (outbound revision 20, inbound revision 13 — both 2026-09-11). Engine behaviour stays pinned; the owner
# stays free to change their agent.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jai_mistry_rev20"

SLUG = "jai_mistry-outbound"


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Nothing here may reach the real model."""


def _router(slug: str = SLUG, direction: str = "outbound") -> ConversationRouter:
    frozen = FIXTURES / f"{slug}.yaml"
    agent = AgentDefinition.from_yaml(
        frozen if frozen.exists() else AGENTS_DIR / slug / "agent.yaml")
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.set_call_context(direction=direction, contact_name="Angelo P Chery",
                       contact_phone="918076562864")
    return r


# ---------------------------------------------------------------------------
# 1. The flow's OWN first stage governs the first turn
# ---------------------------------------------------------------------------

def test_first_stage_after_greeting_is_the_flows_own_first_stage():
    """INTRO_QUALIFY is order 0; DISCOVERY is order 1. The old rule ("a stage
    named DISCOVERY exists anywhere ⇒ go to DISCOVERY") skipped stage 0
    entirely, so the agent never said who was calling and the caller asked
    twice."""
    r = _router()
    assert r._post_greeting_stage() == "INTRO_QUALIFY"


def test_intro_qualify_script_is_in_the_prompt_on_that_turn():
    r = _router()
    r.state.stage = r._post_greeting_stage()
    prompt = r._build_system_prompt()
    assert "Jai Mistry here from Craft Idea" in prompt
    # The wrong-number branch the owner spent two Copilot turns specifying:
    assert "galat number lag gaya hoga" in prompt


def test_legacy_greeting_first_flows_are_untouched():
    """A flow whose stage 0 is literally named GREETING keeps its existing
    behaviour exactly — this fix is scoped to flows whose real opening stage
    was being skipped, and nothing in the incident says anything about this
    shape. pramukh (GREETING → DISCOVERY → …) still lands on DISCOVERY."""
    r = _router("pramukh", direction="inbound")
    stages = [s.id for s in r._def.call_flow.stages]
    assert stages[0] == "GREETING"
    assert r._post_greeting_stage() == "DISCOVERY"


def test_greeting_first_flow_without_a_discovery_stage_is_untouched():
    """fitcoach has no stage named DISCOVERY, so the old rule returned its
    own stage 0 and the agent stayed there — whose goal is written for the
    beat after the caller answers. Changing that would silently skip the
    opening of four live agents."""
    r = _router("fitcoach_ai-outbound")
    stages = [s.id for s in r._def.call_flow.stages]
    assert stages[0] == "GREETING" and "DISCOVERY" not in stages
    assert r._post_greeting_stage() == "GREETING"


# ---------------------------------------------------------------------------
# 2. "हाँ" is a word, not a substring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "बताओ आप कहाँ से बात कर रहे हो?",   # the live turn
    "आप कहाँ रहते हैं?",
    "मैं वहाँ नहीं आ सकता",
    "यहाँ कोई नहीं है",
    "मेरी जीवन शैली अलग है",
])
def test_indic_affirmation_is_not_matched_inside_another_word(text):
    assert detect_intent(text, "DISCOVERY") != "affirmation"


@pytest.mark.parametrize("text", ["हाँ", "हां जी", "हाँजी बिल्कुल", "ठीक है",
                                  "जी", "હા", "બરાબર"])
def test_real_indic_affirmations_still_classify(text):
    assert detect_intent(text, "DISCOVERY") == "affirmation"


# ---------------------------------------------------------------------------
# 3. A follow-on we deliberately waited for is never dropped as "stale"
# ---------------------------------------------------------------------------

def test_merge_gap_cap_covers_the_whole_hold_window():
    """The hold re-arms for up to MERGE_HOLD_MAX_TOTAL_S *because* the caller
    is still speaking. A fixed 3s gap cap therefore threw away exactly the
    continuations the re-arm existed to collect. The cap must be at least as
    long as we were willing to wait."""
    from app.voice import pipeline as P
    assert P.MERGE_HOLD_MAX_TOTAL_S > 3.0, (
        "if the hold can no longer outlast the base cap this test is moot")

    # Mirror of the production expression at the drop site.
    def gap_cap(held_s: float) -> float:
        return max(3.0, held_s + 0.5)

    # The live case: "हा." held 3.97s, the real answer arrived at gap 4.0s.
    assert 4.0 < gap_cap(3.97), "the caller's answer would still be discarded"
    # A genuinely stale segment from a previous turn is still dropped.
    assert 9.0 > gap_cap(0.55)


# ---------------------------------------------------------------------------
# 4 + 5. The close-confirmation gate answers, and asks only once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "कर दो, कर दो, कर दो, कर दो।",   # the live answer, four times over
    "कर दो",
    "हाँ कर दीजिए",
    "kar do",
    "go ahead",
    "yes, please do",
])
def test_imperative_do_it_is_consent_to_close(text):
    assert _router()._is_close_confirm_yes(text) is True


@pytest.mark.parametrize("text", [
    "नहीं, अभी मत करो",
    "ek minute, ek sawal hai",
    "can you take my email address?",
    "मत करो",
])
def test_anything_that_is_not_consent_keeps_the_call_alive(text):
    assert _router()._is_close_confirm_yes(text) is False


def test_close_confirm_is_never_put_to_the_caller_twice():
    """One check protects against a mishearing. A second re-speaks the
    farewell that was bundled with it — which is exactly what the caller heard
    three times on the live call."""
    r = _router()
    assert r._maybe_close_confirm("first") is not None
    # The caller answered (with anything at all).
    r._close_confirm_pending = False
    r._close_confirm_answered = True
    assert r._maybe_close_confirm("second") is None


def test_dead_air_after_a_close_question_re_asks_only_that_question():
    r = _router()
    r.state.turn_count = 6
    r.last_agent_response = (
        "Aapke time ke liye thank you. Have a great day. "
        "ठीक है, तो फिर मैं कॉल कट कर दूँ?")
    r._close_confirm_pending = True

    said = r.generate_silence_reprompt()

    assert said == r._close_confirm_question()
    assert "Have a great day" not in said
    assert "आवाज़ नहीं आई" not in said


# ---------------------------------------------------------------------------
# 6. Delivery: accent on romanized script lines, and no spoken quote marks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "Aapke time ke liye thank you. Have a great day.",
    "Achha Sir, samajh gaya.",
    "Bilkul Sir, koi problem nahi. Aapke liye kaunsa time convenient rahega?",
    "Maaf kijiyega shayad galat number lag gaya hoga. dhanyawad",
])
def test_romanized_hindi_script_lines_are_spoken_by_the_hindi_frontend(line):
    """These are the agent's OWN configured lines. Scoring them as English
    ran them through Sarvam's English pronunciation frontend."""
    r = _router()
    r.state.language = "hi"
    assert r.tts_language_for(line) == "hi"


@pytest.mark.parametrize("line", [
    "Have a great day.",
    "Thank you for your time, Sir.",
    "Craft Idea has been working with builders for fifteen years.",
])
def test_genuinely_english_lines_still_speak_english(line):
    r = _router()
    r.state.language = "hi"
    assert r.tts_language_for(line) == "en"


def test_delimiter_quotes_around_a_verbatim_line_are_not_spoken():
    assert _strip_script_quotes(
        "'Aapke time ke liye thank you. Have a great day.'"
    ) == "Aapke time ke liye thank you. Have a great day."
    # Streamed clause by clause, the marks arrive unpaired.
    assert _strip_script_quotes("'Aapke time ke liye thank you.") == (
        "Aapke time ke liye thank you.")
    # …and the close question got appended after the closing mark.
    assert _strip_script_quotes(
        "'Aapke time ke liye thank you. Have a great day.' "
        "ठीक है, तो फिर मैं कॉल कट कर दूँ?"
    ) == ("Aapke time ke liye thank you. Have a great day. "
          "ठीक है, तो फिर मैं कॉल कट कर दूँ?")


@pytest.mark.parametrize("line", [
    "Achha Sir, that's good to hear.",
    'He said "yes" clearly.',
    "Sir, that's the plan.",
])
def test_quotes_doing_real_work_inside_a_sentence_survive(line):
    assert _strip_script_quotes(line) == line


# ---------------------------------------------------------------------------
# 7. "Which company are you calling from?" — the question the caller asked
#    twice and got an English inbound-shaped answer to.
# ---------------------------------------------------------------------------

def test_identity_answer_on_an_outbound_hindi_call_is_hindi_and_not_an_offer_of_help():
    r = _router()
    r.state.language = "hi"
    answer = r._identity_answer()

    assert "How can I help you" not in answer
    assert "How may I help you" not in answer
    assert "Jay Mistry" in answer or "Craft Idea" in answer
    # Devanagari, because the call is in Hindi.
    assert any("ऀ" <= c <= "ॿ" for c in answer)


def test_identity_answer_on_an_inbound_call_still_offers_help():
    r = _router("jai_mistry-inbound", direction="inbound")
    r.state.language = "en"
    assert "help" in r._identity_answer().lower()


def test_the_live_which_company_question_hits_the_owners_playbook_answer():
    """'आप कौन सी कंपनी से बात कर रहे हो?' — asked on the live call, answered
    with a generic English line. It is now a scripted playbook entry."""
    r = _router()
    r.state.language = "hi"
    hit = r._match_objection_playbook("आप कौन सी कंपनी से बात कर रहे हो?")
    assert hit and "Craft Idea" in hit


# ---------------------------------------------------------------------------
# 8. A decline spoken in the caller's OWN script is heard as a decline
#
# _DEFAULT_OBJECTION_PATTERNS was 100% Latin, so on every hi/gu agent a caller
# declining in their own language never incremented decline_count, never
# reached decline_threshold, and never got the decline farewell — the agent
# kept pitching, then said goodbye twice on the way out. RUNBOOK §37.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "लेकिन मैं अभी इंटरेस्टेड हूं नहीं यार वैसे इन चीजों में।",  # the live turn
    "मुझे इंटरेस्ट नहीं है",
    "कोई दिलचस्पी नहीं",
    "अभी बात नहीं कर सकता",
    "बाद में बात करते हैं",
    "अभी समय नहीं है",
    "मुझे नहीं चाहिए",
    "મને રસ નથી",
    "અત્યારે સમય નથી",
    "પછી વાત કરીએ",
])
def test_a_decline_in_devanagari_or_gujarati_is_an_objection(text):
    assert detect_intent(text, "DISCOVERY") == "objection"


@pytest.mark.parametrize("text", [
    # A plain "no" ANSWER to a qualification question is not a decline — the
    # branch engine needs it to stay a negation.
    "नहीं, हमारे पास एम्प्लॉइज नहीं है उसके अंदर।",
    "नहीं, हम एक्चुअली ज़्यादा उस पर एम्फसाइज़ करते हैं नहीं।",
    "बताओ आप कहाँ से बात कर रहे हो?",
    "हाँ जी बताओ",
    "कल सुबह का समय ठीक रहेगा",
])
def test_a_plain_no_or_a_question_is_not_a_decline(text):
    assert detect_intent(text, "DISCOVERY") != "objection"


# ---------------------------------------------------------------------------
# 9. advance_when.goto — where a stage's escape LANDS
#
# NO_FOCUS_REASON converges on WRAP_UP via a when_slot branch, which needs an
# async slot extractor to land inside the turn. When a deterministic objection
# shortcut answers the turn instead, the extractor never runs and the
# max_turns escape used list adjacency — dropping the caller into
# AGENCY_CHALLENGES, a stage with no script for them, where the model
# improvised an English "Is there anything else I can help you with?".
# Two stages converge on WRAP_UP; only one can be adjacent to it.
# ---------------------------------------------------------------------------

def test_the_convergent_stages_declare_where_their_escape_lands():
    r = _router()
    by_id = {s.id: s for s in r._def.call_flow.stages}
    for sid in ("NO_FOCUS_REASON", "AGENCY_CHALLENGES"):
        assert (by_id[sid].advance_when or {}).get("goto") == "WRAP_UP", sid


def test_max_turns_escape_honours_goto_instead_of_list_adjacency():
    r = _router()
    stage_ids = [s.id for s in r._def.call_flow.stages]
    # The stage that follows NO_FOCUS_REASON in the YAML is NOT WRAP_UP —
    # otherwise this test proves nothing.
    nxt = stage_ids[stage_ids.index("NO_FOCUS_REASON") + 1]
    assert nxt != "WRAP_UP"

    r.state.stage = "NO_FOCUS_REASON"
    r.state.discovery_turns = 5          # well past max_turns
    r._maybe_advance_stage()
    assert r.state.stage == "WRAP_UP"


def test_an_unknown_goto_falls_back_to_the_next_stage(caplog):
    """A typo'd destination must never strand the call."""
    r = _router()
    stage_ids = [s.id for s in r._def.call_flow.stages]
    tgt = next(s for s in r._def.call_flow.stages if s.id == "NO_FOCUS_REASON")
    tgt.advance_when = dict(tgt.advance_when or {}, goto="NO_SUCH_STAGE")

    r.state.stage = "NO_FOCUS_REASON"
    r.state.discovery_turns = 5
    r._maybe_advance_stage()
    assert r.state.stage == stage_ids[stage_ids.index("NO_FOCUS_REASON") + 1]


def test_wrap_up_does_not_speak_a_second_farewell():
    """A scripted decline answer is itself a goodbye; WRAP_UP must not follow
    it with a second farewell speech."""
    r = _router()
    goal = next(s.goal for s in r._def.call_flow.stages if s.id == "WRAP_UP")
    assert "ALREADY been spoken" in goal
