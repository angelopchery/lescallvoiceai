"""Budget-fit suggestion + caller-confirmed pitch switch (RUNBOOK §68).

Built for the seven Pramukh per-project agents (2026-10-07): each pitches its
own project like Ravi pitches Swarit, shares one catalogue, and — when the
caller's budget does not fit — may suggest the projects that do, switching
its pitch only after the caller explicitly says yes.

Offline: no LLM calls, no live agent YAML (frozen fixture).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_definition.catalog import render_catalog_prompt
from app.agent_definition.controls import unknown_paths
from app.agent_definition.schema import AgentDefinition
from app.voice import pitch_switch as ps
from app.voice.conversation import ConversationRouter

FIX = Path(__file__).parent / "fixtures" / "pramukh_swarit_akshay_outbound_2026_10_07.yaml"


@pytest.fixture()
def agent():
    return AgentDefinition.from_yaml(FIX)


@pytest.fixture()
def router(agent):
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.state.stage = "SOFT_QUALIFICATION"
    return r


def _turn_band(r) -> str:
    p = r._build_system_prompt()
    return p[p.find("━━ CALL STATE ━━"):]


# ── money parsing ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("50 Lakhs", [50.0]),
    ("1.5 Crore", [150.0]),
    ("50-60 Lakhs", [50.0, 60.0]),
    ("₹2.15 Cr – ₹3.70 Cr", [215.0, 370.0]),
    ("₹95 lakh – ₹1.20 Cr", [95.0, 120.0]),
    ("2,320 sq ft 90 lakh", [90.0]),      # sizes are not money
    ("3 BHK 80 lakh", [80.0]),
    ("डेढ़ करोड़", [150.0]),
    ("ढाई करोड़ तक", [250.0]),
    ("1,20,00,000", [120.0]),             # rupees
])
def test_amounts(text, expected):
    assert ps.amounts_lakhs(text) == expected


def test_fit_tolerance():
    assert ps.fits((150, 150), (175, 175))          # rounding down is not a miss
    assert not ps.fits((100, 100), (215, 370))
    assert ps.fits((100, 100), (78, 112))


_YES = ["yes", "Yeah sure", "ok", "okay tell me", "sure, go ahead",
        "sounds good", "I'm interested", "why not", "of course",
        "no problem, tell me", "definitely", "please", "haan", "haan ji", "ji",
        "ji haan", "theek hai", "thik hai batao", "bilkul", "chalega",
        "zaroor batao", "bataiye", "bataiye na", "bolo na", "haan bolo",
        "accha", "achha batao", "kyu nahi", "koi baat nahi, bataiye",
        "हाँ", "हां जी", "जी", "जी हाँ", "ठीक है", "बिल्कुल", "चलेगा", "ज़रूर",
        "बताइए", "बताइए ना", "हाँ बताइए", "अच्छा बताइए", "क्यों नहीं", "बढ़िया",
        "सही है", "ओके", "हाँ जी बिल्कुल बताइए", "हा", "હા", "હા જી", "બરાબર",
        "ચોક્કસ", "ચાલશે", "કહો", "કહોને", "સારું", "ઠીક છે", "કેમ નહીં", "જરૂર કહો"]
_NO = ["no", "nope", "no thanks", "not interested", "nahi", "nahi chahiye",
       "na", "ना", "नहीं", "नहीं चाहिए", "रहने दो", "जी नहीं", "ના",
       "નથી જોઈતું", "haan but no", "rather not"]
_OTHER = ["maybe", "let me think", "sochta hoon", "baad mein", "dekhenge",
          "शायद", "पता नहीं", "hmm", "what is the price?", "kitne ka hai",
          "thank you", "પછી"]


@pytest.mark.parametrize("text", _YES)
def test_every_positive_reply_is_yes(text):
    assert ps.classify_answer(text) == "yes"


@pytest.mark.parametrize("text", _NO)
def test_negative_reply_is_no(text):
    assert ps.classify_answer(text) == "no"


@pytest.mark.parametrize("text", _OTHER)
def test_hedge_or_question_is_not_an_answer(text):
    assert ps.classify_answer(text) == "other"


# ── data shape ──────────────────────────────────────────────────────────────
def test_fixture_is_known_shape(agent):
    assert agent.knowledge_base.primary_project["budget_switch"] is True
    assert not unknown_paths(["knowledge_base.primary_project.budget_switch"])
    assert len(agent.knowledge_base.catalog) == 7
    assert all(ps.price_bounds(i) for i in agent.knowledge_base.catalog)


def test_static_band_names_the_exception(agent):
    txt = render_catalog_prompt(agent.knowledge_base)
    assert "BUDGET FIT" in txt and "★ PRIMARY PROJECT" in txt
    assert "budget_lakhs" not in txt          # data, never prompt


def test_no_flag_no_band(agent):
    agent.knowledge_base.primary_project.pop("budget_switch")
    r = ConversationRouter(agent_def=agent, default_language="hi")
    assert r._pitch is None
    r.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    assert "Their range (" not in r._build_system_prompt()


# ── the flow ────────────────────────────────────────────────────────────────
def test_fitting_budget_no_suggestion(router):
    router.state_mgr.observe_user("ढाई करोड़ तक", asked="budget_range")
    assert "Their range (" not in _turn_band(router)


def test_mismatch_suggests_fitting_projects(router):
    """Owner (2026-10-12): first the generic question — "would you like to
    see our other properties in this budget?" — the names only after a yes."""
    router.state_mgr.observe_user("हमारा budget 1 करोड़ है", asked="budget_range")
    band = _turn_band(router)
    assert "BUDGET FIT — suggest" in band
    first = band.split("BUDGET FIT — suggest")[1].split("▸ It is a suggestion")[0]
    assert "other properties in that budget" in first
    assert "Pramukh Green County" not in first and "Pramukh Silver Stone" not in first
    router._store_last_response(
        "Swarit इस range से ऊपर है — क्या आप इस budget में हमारी और properties "
        "देखना चाहेंगे?")
    router._pitch_observe_caller("हाँ, बताइए")
    band = _turn_band(router)
    assert "said YES" in band
    assert "Pramukh Green County" in band and "Pramukh Silver Stone" in band
    assert "Pramukh Vivan" not in band.split("BUDGET FIT")[1]


def test_question_about_other_project_never_switches(router):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._pitch_observe_caller("Vivan kitne ka hai?")
    assert router._pitch.active_id == "pramukh-swarit"


def test_yes_to_one_offer_switches(router):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._store_last_response(
        "Swarit थोड़ा ऊपर है, पर Green County आपके budget में बैठता है — बताऊँ?")
    assert router._pitch.offered == ["pramukh-green-county"]
    router._pitch_observe_caller("हाँ जी बताइए")
    assert router._pitch.active_id == "pramukh-green-county"
    assert router.state.project_recommended == "Pramukh Green County"
    router.state.stage = "PRESENT_PROJECT"
    band = _turn_band(router)
    assert "PITCH SWITCHED" in band
    assert "PRIMARY PROJECT: Pramukh Green County" in band
    goal = band[band.find("━━ CURRENT GOAL ━━"):]
    goal = goal[:goal.find("━━", 20)]
    assert "Swarit" not in goal and "Green County" in goal


def test_plain_yes_to_two_offers_asks_which(router):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._store_last_response("Green County या Silver Stone — किसके बारे में बताऊँ?")
    router._pitch_observe_caller("haan")
    assert router._pitch.active_id == "pramukh-swarit"
    assert "ask, in a few words, which one" in _turn_band(router)
    router._pitch_observe_caller("Silver Stone batao")
    assert router._pitch.active_id == "pramukh-silver-stone"


def test_no_declines_and_never_resuggests(router):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._store_last_response("Green County आपके budget में बैठता है — बताऊँ?")
    router._pitch_observe_caller("नहीं, Swarit ही देखना है")
    assert router._pitch.active_id == "pramukh-swarit"
    band = _turn_band(router)
    assert "Their range (" not in band and "You suggested" not in band
    assert "pramukh-green-county" in router._pitch.declined


def test_unanswered_offer_expires(router):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._store_last_response("Green County आपके budget में बैठता है — बताऊँ?")
    router._pitch_observe_caller("hmm")
    router._pitch_observe_caller("family mein chaar log hain")
    assert router._pitch.offered == []
    assert router._pitch.active_id == "pramukh-swarit"


def test_new_budget_after_decline_can_suggest_again(router):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._store_last_response("Green County आपके budget में बैठता है — बताऊँ?")
    router._pitch_observe_caller("nahi")
    assert "Their range (" not in _turn_band(router)
    router.state_mgr.observe_user("actually 50 lakh tak hi hai", asked="budget_range")
    band = _turn_band(router)
    assert "Their range (₹50 lakh)" in band
    router._store_last_response(
        "क्या आप इस budget में हमारी और properties देखना चाहेंगे?")
    router._pitch_observe_caller("haan")
    assert "Pramukh Blue Stone" in _turn_band(router)


@pytest.mark.parametrize("reply", ["okay", "sure, tell me", "chalega",
                                   "ठीक है", "બરાબર", "why not",
                                   "Green County chalega", "Green County"])
def test_any_positive_reply_switches(router, reply):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._store_last_response("Green County आपके budget में बैठता है — बताऊँ?")
    router._pitch_observe_caller(reply)
    assert router._pitch.active_id == "pramukh-green-county"


def test_question_about_offer_keeps_it_open(router):
    router.state_mgr.observe_user("budget 1 crore", asked="budget_range")
    _turn_band(router)
    router._store_last_response("Green County आपके budget में बैठता है — बताऊँ?")
    for q in ("Green County kitne ka hai?", "wahan parking kitni hai?",
              "aur amenities kya hain?"):
        router._pitch_observe_caller(q)
        assert router._pitch.active_id == "pramukh-swarit"
        assert router._pitch.offered == ["pramukh-green-county"]
    router._pitch_observe_caller("theek hai, chalega")
    assert router._pitch.active_id == "pramukh-green-county"
