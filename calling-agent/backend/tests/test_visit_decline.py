"""A declined site visit stands; an investor is never asked who will live
there (RUNBOOK §73, 2026-10-12).

Pramukh test calls on 2026-10-11 (arisha@ / shubhangi@):

  * six of seven calls re-invited after "I can't come now" — the INVITE
    stage's own "photos and videos only show so much" script, turn after
    turn, until a caller asked why the agent kept saying it (1058);
  * "अभी नहीं" is also a universal objection pattern, so the decline got
    the canned "I won't take much time — when should I call?" (1060, 1061);
  * "investment" was recorded on turn 4 and "who will be living there?"
    still followed (1057, 1060, 1064);
  * a QUESTION about living there flipped Investment to Self-use (1060);
  * dead-air repair replayed the refused invitation (1059).

Offline: no LLM calls, no live agent YAML.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import conversation as conv
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


INVITE = ("फोटो और वीडियो से तो बस एक अंदाज़ा मिलता है मैडम, तो आप कब आना "
          "चाहेंगी — इस हफ्ते किसी शाम को या वीकेंड पर?")
CANNED = "बिल्कुल, मैं ज़्यादा टाइम नहीं लूँगा। या अगर अभी ठीक नहीं है तो बताइए कब कॉल करूँ।"


def _slot(name, prio, typ="text", enum=None, syn=None, required=True,
          merge="free", q=None):
    return {"name": name, "priority": prio, "required": required,
            "label": name, "question": q or {"hi": f"{name}?", "en": f"{name}?"},
            "type": typ, "enum_values": enum or [], "enum_synonyms": syn or {},
            "extract_patterns": [], "depends_on": [], "merge_policy": merge,
            "confirm_readback": "", "max_asks": 1, "reask_question": {},
            "summary_known": "", "summary_unknown": ""}


def make_router(stage="INVITE_SITE_VISIT"):
    d = {
        "agent_id": "t_visit", "version": "1.0.0",
        "identity": {"agent_name": "Prashant", "company_name": "Pramukh Aristo"},
        "languages": {"supported": ["hi", "en"], "default": "hi",
                      "tts_voices": {}},
        "call_flow": {
            "stages": [
                {"id": "DISCOVERY", "order": 0, "goal": "learn about them",
                 "role": "discovery", "turns_until_advance": 4,
                 "collect": ["purpose", "family_size"],
                 "advance_when": {"all_collect_filled": True, "max_turns": 4},
                 "branches": [{"when_slot": {"name": "purpose",
                                             "equals": "Investment"},
                               "max_takes": 1,
                               "actions": ["retire_slots:family_size"]}]},
                {"id": "INVITE_SITE_VISIT", "order": 1, "goal": "invite them",
                 "role": "scheduling", "turns_until_advance": 4,
                 "advance_when": {"appointment_captured": True, "max_turns": 4},
                 "branches": [
                     {"when_slot": {"name": "site_visit_interest",
                                    "equals": v},
                      "goto": "CONFIRM_AND_CLOSE", "max_takes": 1,
                      "actions": []} for v in ("Maybe later", "No")]},
                {"id": "CONFIRM_AND_CLOSE", "order": 2, "goal": "wrap up",
                 "role": "confirmation", "turns_until_advance": 99}],
            "decline_threshold": 2,
        },
        "slots": [
            _slot("purpose", 1, "enum", ["Self-use", "Investment"],
                  {"Self-use": ["रहने के लिए", "khud ke liye", "for myself"],
                   "Investment": ["investment", "इन्वेस्टमेंट", "निवेश"]},
                  merge="correction_gated",
                  q={"hi": "ये घर आप ख़ुद रहने के लिए देख रहे हैं, या investment "
                           "के तौर पर?", "en": "For yourself, or investment?"}),
            _slot("family_size", 2, q={"hi": "और वहाँ कौन-कौन रहेगा — आप कितने "
                                             "लोग हैं?", "en": "Who all?"}),
            _slot("site_visit_interest", 3, "enum",
                  ["Yes", "Maybe later", "No"],
                  {"Yes": ["haan", "sure"], "Maybe later": ["baad mein"],
                   "No": ["nahi"]}, required=False,
                  q={"hi": "क्या आप site देखने आना चाहेंगे?",
                     "en": "Would you like to visit the site?"}),
        ],
        "response_bank": {"opening_greeting": "hi",
                          "objection_responses": {"hi": [CANNED]}},
    }
    agent = AgentDefinition.from_dict(d)
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.state.language = "hi"
    r.state.turn_count = 5
    r.state.stage = stage
    r.last_agent_response = INVITE
    return r


def drive(r, text):
    async def _one():
        return [res async for res in r.process_stream(text)]
    return asyncio.run(_one())


def said(results) -> str:
    return " ".join(getattr(x, "text", "") or "" for x in results).strip()


# ── the detector, on the live lines ─────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("अभी तो नहीं देखूंगी आकर।", "Maybe later"),                       # 1057
    ("लेकिन बाद में शायद आऊंगी।", "Maybe later"),
    ("मुझे नहीं आना देखना है, आप क्यों बार बार बोल रहे हो?", "No"),    # 1058
    ("मैंने बोला कि मुझे नहीं देखना।", "No"),                           # 1059
    ("नहीं आना भाई मेरे को, तू पहले मेरे को बता तो दे थोड़ा।", "Maybe later"),  # 1063
    ("अभी नहीं, अभी आप मुझे पूरे फीचर्स बताइए", "Maybe later"),        # 1060
    ("जी मैं अभी कंफर्टेबल नहीं हूँ।", "Maybe later"),
    ("नहीं।", "Maybe later"),                                           # 1059
    ("नहीं, अभी मैं फ्री नहीं हूँ।", "Maybe later"),                     # 1068
    ("चलो मैं देखता हूँ जैसे कोई प्रोग्राम बनता है फिर बताता हूँ।", "Maybe later"),
    ("i can't come right now", "Maybe later"),
    ("not now, maybe later", "Maybe later"),
    ("i won't be able to visit", "No"),
    ("पहले थोड़ी बताओ तो सही प्रॉपर्टी के बारे में।", "answer_first"),  # 1063
    ("नहीं, पहले मुझे थोड़ा प्रॉपर्टी के बारे में बताओ।", "answer_first"),
    ("first tell me about the property", "answer_first"),
])
def test_declines_after_an_invitation(text, expected):
    r = make_router()
    assert r._detect_visit_decline(text) == expected


@pytest.mark.parametrize("text", [
    "सुबह नहीं आ सकते।",                        # picking a time (1031)
    "cannot make it on saturday, only after 5 pm",
    "नहीं, आप एक काम कीजिए, मेरी 14th ऑक्टोबर।",
    "अब नहीं आया।",                              # the offer letter (934)
    "हाँ, वीकेंड पर आऊंगी",
    "हाँ ज़रूर",
    "नहीं नहीं, 4 bhk नहीं, मैं 3 bhk बोल रहा हूँ।",   # a correction
    "no, i want properties in vapi",
    "नहीं नहीं नहीं, Blue Stone भी तो देखना है।",
    "बजट नहीं बताया भाई आपने।",
    "अभी रेडी नहीं है इसमें, पजेशन नहीं मिल जाएगा अभी।",
    "स्क्वायर फीट क्या है वह बताइए ना जरा।",
    "कितने बजे आऊं?",
])
def test_not_a_decline(text):
    r = make_router()
    assert r._detect_visit_decline(text) == ""


def test_weak_forms_need_an_invitation_in_the_previous_line():
    r = make_router()
    r.last_agent_response = "आप किस size का सोच रहे हैं — कितने BHK?"
    assert r._detect_visit_decline("अभी नहीं") == ""
    assert r._detect_visit_decline("नहीं।") == ""
    # a strong form stands on its own
    assert r._detect_visit_decline("मुझे नहीं आना देखना है") == "No"


def test_the_agents_own_visit_question_counts_as_an_invitation():
    r = make_router()
    r.last_agent_response = "क्या आप site देखने आना चाहेंगे?"
    assert r._detect_visit_decline("नहीं") == "Maybe later"


# ── what the engine does with it ────────────────────────────────────────────

def test_a_visit_decline_is_not_an_objection_strike():
    r = make_router()
    out = said(drive(r, "अभी नहीं आ सकती"))
    assert CANNED not in out and "ज़्यादा टाइम" not in out
    assert r.state.decline_count == 0
    assert r.state_mgr.state.get("site_visit_interest") == "Maybe later"
    assert r.state_mgr.extra_flags.get("visit_declined") is True


def test_the_decline_leaves_the_invite_stage_at_once():
    r = make_router()
    drive(r, "अभी नहीं आ सकती")
    assert r.state.stage == "CONFIRM_AND_CLOSE"


def test_the_band_tells_the_model_what_to_do_and_persists():
    r = make_router()
    drive(r, "अभी नहीं आ सकती")
    p = r._build_system_prompt()
    assert "━━ VISIT DECLINED ━━" in p
    assert "WhatsApp" in p and "Do NOT invite them again" in p
    drive(r, "ठीक है")
    p2 = r._build_system_prompt()
    assert "━━ VISIT DECLINED ━━" in p2
    assert "declined a site visit earlier" in p2


def test_a_decline_of_the_call_keeps_the_objection_path():
    r = make_router()
    out = said(drive(r, "अभी नहीं, मुझे इंटरेस्ट नहीं है"))
    assert r.state.decline_count == 1
    assert CANNED in out


def test_a_hard_no_never_softens_back_to_maybe():
    r = make_router()
    drive(r, "मुझे नहीं आना देखना है")
    assert r.state_mgr.state.get("site_visit_interest") == "No"
    r.last_agent_response = INVITE
    drive(r, "बाद में देखेंगे")
    assert r.state_mgr.state.get("site_visit_interest") == "No"


def test_tell_me_first_holds_the_invitation_for_one_turn_only():
    r = make_router()
    drive(r, "पहले थोड़ी बताओ तो सही प्रॉपर्टी के बारे में।")
    assert r.state_mgr.state.get("site_visit_interest") is None
    assert "visit_declined" not in r.state_mgr.extra_flags
    assert "━━ ANSWER FIRST ━━" in r._build_system_prompt()
    assert r.state.stage == "INVITE_SITE_VISIT"
    drive(r, "अच्छा")
    assert "━━ ANSWER FIRST ━━" not in r._build_system_prompt()


def test_dead_air_repair_never_replays_the_refused_invitation():
    r = make_router()
    drive(r, "अभी नहीं आ सकती")
    line = "ठीक है मैडम, मैं Pramukh Aristo की details WhatsApp कर देता हूँ।"
    r.memory.add_assistant(line, language="hi")
    r._store_last_response(line)
    re_ask = r.generate_silence_reprompt()
    assert "वीकेंड" not in re_ask and "आना" not in re_ask
    assert "लाइन पर" in re_ask


def test_dead_air_repair_still_repeats_an_unanswered_question():
    r = make_router()
    q = "आप किस size का सोच रहे हैं — कितने BHK?"
    r.memory.add_user("हेलो")
    r.memory.add_assistant(q, language="hi")
    r._store_last_response(q)
    r.state.turn_count = 3
    assert "कितने BHK" in r.generate_silence_reprompt()


# ── an investor is never asked who will live there ──────────────────────────

def test_investment_retires_family_size_without_moving_the_stage():
    r = make_router(stage="DISCOVERY")
    r.last_agent_response = "ये घर आप ख़ुद रहने के लिए देख रहे हैं, या investment के तौर पर?"
    drive(r, "investment ke liye dekh raha hoon")
    assert r.state_mgr.state.get("purpose") == "Investment"
    assert "family_size" in r.state_mgr.retired
    assert "family_size" not in r.state_mgr.detect_missing_slots()["missing"]


def test_a_question_about_living_there_does_not_flip_a_recorded_purpose():
    r = make_router(stage="DISCOVERY")
    r.last_agent_response = "ये घर आप ख़ुद रहने के लिए देख रहे हैं, या investment के तौर पर?"
    drive(r, "इन्वेस्टमेंट के लिए")
    assert r.state_mgr.state.get("purpose") == "Investment"
    r.last_agent_response = "Pramukh Aristo में 3 BHK है मैडम।"
    drive(r, "वहाँ पर रहने के लिए क्या-क्या एडवांटेजेस हैं")
    assert r.state_mgr.state.get("purpose") == "Investment"
    # an explicit correction still lands
    drive(r, "नहीं, मुझे रहने के लिए चाहिए")
    assert r.state_mgr.state.get("purpose") == "Self-use"


def test_an_action_only_branch_runs_once():
    r = make_router(stage="DISCOVERY")
    r.last_agent_response = "ये घर आप ख़ुद रहने के लिए देख रहे हैं, या investment के तौर पर?"
    drive(r, "investment ke liye")
    takes = dict(r._branch_takes)
    drive(r, "haan investment")
    assert r._branch_takes == takes
