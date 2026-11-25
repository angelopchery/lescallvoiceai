"""Who is addressed, and who is speaking — RUNBOOK §42 (2026-09-11).

Two platform rules, for every agent in every account, existing and future:

  1. The CALLER is addressed by their contact record. Sir/Ma'am and Mr./Mrs.
     come from contacts.gender; with no gender on file the agent uses NO
     gendered address at all — not through a placeholder, not through a script
     that hardcoded "Sir", not through the model improvising one. Measured on
     prod before the fix: jai_mistry-2 greeted two women "SHAHANAZ Sir" /
     "SHREE Sir", maya_lescall_sdr called one caller "Mrs. Paul" then
     "Mr. Paul", and the {contact_name} fallback spoke "Am I speaking to
     sir/ma'am?" on 29+ lines.
  2. The AGENT speaks in the gender of the voice the business selected. Seven
     shubh agents carried "she"; pramukh_agent's Vishal Patel said "बात कर रही
     हूँ", and jai_mistry-2 (switched to simran) kept saying "samajh gaya".

The false-positive guards matter as much as the rules: every regendering rule
is anchored on a form only the speaker can own, and every address rule on
script-aware word boundaries — सर्विसेज, अक्सर and ખૂબ સરસ all contain the
letters of "Sir".
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb
from app.voice.address import (
    HONORIFIC_PREVIEW_GENDER, agent_gender, enforce_address, neutral_address,
    protected_names, regender_self, render_author_line, resolve_contact_name,
    tokenize_address, tokenize_spoken_fields, voice_gender)
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


# ── 2. the agent's own first person follows the voice ──────────────────────

@pytest.mark.parametrize("masc,fem", [
    ("kya main Rajesh se baat kar raha hoon?", "kya main Rajesh se baat kar rahi hoon?"),
    ("Main bas ek quick sa check karna chahta tha — kya aap",
     "Main bas ek quick sa check karna chahti thi — kya aap"),
    ("Achha Sir, samajh gaya.", "Achha Sir, samajh gayi."),
    ("Main aapki details note kar leta hoon.", "Main aapki details note kar leti hoon."),
    ("Main aapko kal call karunga.", "Main aapko kal call karungi."),
    ("details bhej dunga", "details bhej dungi"),
    ("जी, मैं नोट कर रहा हूँ — पूरा ईमेल बता दीजिए।",
     "जी, मैं नोट कर रही हूँ — पूरा ईमेल बता दीजिए।"),
    ("जी, समझ गया — मैं कैसे मदद कर सकता हूँ?",
     "जी, समझ गई — मैं कैसे मदद कर सकती हूँ?"),
    ("मैं आपको कल कॉल करूँगा।", "मैं आपको कल कॉल करूँगी।"),
    ("मैं बस आपसे थोड़ी बात करना चाहता था।", "मैं बस आपसे थोड़ी बात करना चाहती थी।"),
    ("જી, હું નોંધી રહ્યો છું — પૂરું ઈમેલ કહી દો.",
     "જી, હું નોંધી રહી છું — પૂરું ઈમેલ કહી દો."),
    ("હું આના પર પાછો આવું છું.", "હું આના પર પાછી આવું છું."),
    ("શું હું રાજેશ સાથે વાત કરી રહ્યો છું?", "શું હું રાજેશ સાથે વાત કરી રહી છું?"),
])
def test_first_person_follows_the_voice_both_ways(masc, fem):
    assert regender_self(masc, "female") == fem
    assert regender_self(fem, "male") == masc


def test_a_danda_ends_a_word():
    """"।" lives in the Devanagari block; counted as a letter it hid every
    Hindi sentence that ended on the verb — including the live pramukh line."""
    assert regender_self("मैं Pramukh Group से विशाल पटेल बात कर रही हूँ।", "male") \
        == "मैं Pramukh Group से विशाल पटेल बात कर रहा हूँ।"


@pytest.mark.parametrize("text", [
    "number lag gaya hoga",                      # the number, not the speaker
    "Kya koi particular wajah rahi ab tak?",     # wajah is feminine
    "kya hua?",
    "Craft Idea pichle 15 saal se kaam kar rahi hai.",
    "Us setup mein koi challenge aa raha hai?",
    "maine socha tha ki aap busy honge",         # ergative — no agreement
    "हमारी प्राइसिंग 5,000 से 5 लाख तक हो सकती है।",
    "आपने हमारी प्रॉपर्टीज में इंटरेस्ट दिखाया था।",
    "લાગે છે કે અત્યારે તમારો અવાજ નથી આવી રહ્યો, તો હું કૉલ અહીં મૂકું છું.",
    "આવી વાત નથી",                               # આવી = "such"
    "main Kavita hoon",                          # a name ending in -a
    "main Ananya hoon, Dhronai se",
    "hamare paas cotton lungi bhi hai",          # the garment
])
def test_nothing_that_is_not_first_person_changes(text):
    assert regender_self(text, "female") == text
    assert regender_self(text, "male") == text


def test_unknown_agent_gender_changes_nothing():
    assert regender_self("main kar raha hoon", "") == "main kar raha hoon"


def test_regendering_is_idempotent():
    once = regender_self("Main chahta tha, samajh gaya, kar raha hoon", "female")
    assert regender_self(once, "female") == once


# ── the voice decides the agent's gender ───────────────────────────────────

def _agent(voices: dict | None = None, pronouns: str | None = None,
           default: str = "hi", **over) -> AgentDefinition:
    d = {
        "agent_id": "t_addr", "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["hi", "en"], "default": default,
                      "tts_voices": voices if voices is not None else {}},
        "call_flow": {"stages": [{"id": "S1", "order": 0, "goal": "one",
                                  "turns_until_advance": 8}]},
        "response_bank": {"opening_greeting": "hi"},
    }
    if pronouns is not None:
        d["identity"]["pronouns"] = pronouns
        d["personality"] = {"pronouns": pronouns}
    d.update(over)
    return AgentDefinition.from_dict(d)


def test_voice_gender_reads_the_speaker():
    assert voice_gender(_agent({"hi": {"speaker": "shubh"}})) == "male"
    assert voice_gender(_agent({"hi": {"speaker": "simran"}})) == "female"


def test_voice_gender_follows_the_runtime_voice_chain():
    """That language's voice, else the default language's, else shubh — the
    same chain conversation_call_bridge uses to pick what the caller hears."""
    a = _agent({"hi": {"speaker": "simran"}, "en": {"speaker": "ratan"}})
    assert voice_gender(a, "en") == "male"
    assert voice_gender(a, "hi") == "female"
    assert voice_gender(a, "gu") == "female"         # falls to default (hi)
    assert voice_gender(_agent({})) == "male"          # platform default shubh


def test_unknown_speaker_falls_back_to_stored_pronouns():
    a = _agent({"hi": {"speaker": "newsarvamvoice"}}, pronouns="she")
    assert voice_gender(a) == ""
    assert agent_gender(a) == "female"


@pytest.mark.parametrize("speaker,stored,derived", [
    ("shubh", "she", "he"),      # pramukh_agent, sen_studio, test_nam…
    ("simran", "he", "she"),     # the jai_mistry-2 direction
    ("ishita", "they", "she"),
])
def test_schema_derives_pronouns_from_the_voice(speaker, stored, derived):
    a = _agent({"hi": {"speaker": speaker}}, pronouns=stored)
    assert a.identity.pronouns == derived
    assert a.personality.pronouns == derived


def test_schema_keeps_stored_pronouns_for_an_unclassifiable_voice():
    a = _agent({"hi": {"speaker": "newsarvamvoice"}}, pronouns="she")
    assert a.personality.pronouns == "she"


def test_prompt_pronoun_rule_follows_the_voice_not_the_stored_field():
    """The live bug: a shubh voice told 'ALWAYS use rahi'."""
    male = pb.pronoun_instruction(_agent({"hi": {"speaker": "shubh"}}, pronouns="she"))
    assert male.startswith("Your pronouns are he/him")
    female = pb.pronoun_instruction(_agent({"hi": {"speaker": "simran"}}, pronouns="he"))
    assert female.startswith("Your pronouns are she/her")


# ── 1. the caller is addressed by their record — or not at all ─────────────

@pytest.mark.parametrize("text,expected", [
    ("Achha Sir, samajh gaya.", "Achha, samajh gaya."),
    ("Thank you Sir. Main bas ek check", "Thank you. Main bas ek check"),
    ("Sir, kya ek minute baat kar sakte hain?", "Kya ek minute baat kar sakte hain?"),
    ("जी Sir, मेरी आवाज आ रही है?", "जी, मेरी आवाज आ रही है?"),
    ("Koi baat nahi sir, Aapse baat karke acha laga.",
     "Koi baat nahi, Aapse baat karke acha laga."),
    ("Hello, kya main Shahanaz Sir se baat kar raha hoon?",
     "Hello, kya main Shahanaz se baat kar raha hoon?"),
    ("Am I speaking with Mr. Raj?", "Am I speaking with Raj?"),
    ("Hello, am I speaking to sir/ma'am?", "Hello."),
])
def test_no_gender_on_file_means_no_gendered_address(text, expected):
    name = "Shahanaz" if "Shahanaz" in text else ("Raj" if "Raj" in text else "")
    assert enforce_address(text, "", name) == expected


@pytest.mark.parametrize("text,gender,name,expected", [
    ("Hello, kya main Shahanaz Sir se baat kar raha hoon?", "female", "Shahanaz",
     "Hello, kya main Shahanaz Ma'am se baat kar raha hoon?"),
    ("नमस्ते सर! कैसे हैं?", "female", "", "नमस्ते मैडम! कैसे हैं?"),
    ("धन्यवाद मैडम।", "male", "", "धन्यवाद सर।"),
    ("કેમ છો સર?", "female", "", "કેમ છો મેડમ?"),
    ("Am I speaking with Mrs. Raj?", "male", "Raj", "Am I speaking with Mr. Raj?"),
])
def test_a_known_gender_corrects_the_word_in_its_own_script(text, gender, name, expected):
    assert enforce_address(text, gender, name) == expected


def test_a_title_is_only_theirs_in_front_of_their_own_name():
    """dhronai_ea calls on behalf of 'Mr. Umang, the CEO' — a third party."""
    out = enforce_address("Mr. Umang would love to meet you, Mr. Raj.", "female", "Raj")
    assert out == "Mr. Umang would love to meet you, Mrs. Raj."
    assert enforce_address("Mr. Umang would love to meet you.", "", "Raj") \
        == "Mr. Umang would love to meet you."


def test_a_protected_third_party_keeps_their_sir():
    assert enforce_address("Umang Sir aapse milna chahte hain.", "female", "Priya",
                           protected={"umang"}) == "Umang Sir aapse milna chahte hain."


def test_a_contact_called_shree_is_not_a_title():
    assert enforce_address("Hello Shree Panchal Ma'am", "female", "Shree Panchal") \
        == "Hello Shree Panchal Ma'am"


@pytest.mark.parametrize("text", [
    "हमारी सर्विसेज अक्सर फ्रीलांसर के लिए हैं।",
    "ખૂબ સરસ! એકવાર સરખામણી કરવી ફાયદાકારક રહેશે.",
    "Sirf ek minute chahiye.",
    "The mammoth task is done.",
])
def test_words_that_merely_contain_the_letters_are_untouched(text):
    for g in ("", "male", "female"):
        assert enforce_address(text, g, "Rajesh") == text


def test_a_removed_word_never_leaves_a_sentence_opening_on_a_comma():
    """Live shape: "Jai Mistry here from Craft Idea. {honorific}, kya ek
    minute…" for a contact with no gender came out "Craft Idea., kya"."""
    from app.voice.address import tidy_seams
    assert tidy_seams("Craft Idea. , kya ek minute?") == "Craft Idea. kya ek minute?"
    assert enforce_address("Craft Idea. Sir, kya ek minute?", "", "") \
        == "Craft Idea. kya ek minute?"
    # a closing quote keeps its comma
    assert tidy_seams("he said 'yes', then left") == "he said 'yes', then left"


def test_the_preview_sentinel_is_left_alone():
    assert enforce_address("Thank you Sir", HONORIFIC_PREVIEW_GENDER, "") == "Thank you Sir"


def test_address_enforcement_is_idempotent():
    for g in ("", "male", "female"):
        once = enforce_address("Achha Sir, Mr. Raj, samajh gaya", g, "Raj")
        assert enforce_address(once, g, "Raj") == once


# ── {contact_name} with no name on file ────────────────────────────────────

@pytest.mark.parametrize("line,expected", [
    ("Hello, kya main {contact_name} {honorific} se baat kar raha hoon?", "Hello."),
    ("Hope you're having a very good morning, am I speaking with {contact_name}?",
     "Hope you're having a very good morning."),
    ("Am I speaking to {contact_name}?", ""),
    ("Hello {contact_name}, this is Priya from X calling about your loan.",
     "Hello, this is Priya from X calling about your loan."),
    ("Thank you {contact_name}!", "Thank you!"),
    ("Hello {contact_name}, how are you today?", "Hello, how are you today?"),
    ("શું હું {contact_name} સાથે વાત કરી રહી છું?", ""),
])
def test_no_name_never_becomes_sir_maam(line, expected):
    out = resolve_contact_name(line, "")
    assert out == expected
    assert "sir" not in out.lower() and "ma'am" not in out.lower()


def test_a_known_name_is_simply_filled_in():
    assert resolve_contact_name("Hello {contact_name}!", "Rajesh") == "Hello Rajesh!"


# ── authoring: literal address becomes a placeholder ───────────────────────

def test_tokenize_rewrites_the_jai_mistry_opening():
    line = ("Hello, kya main {contact_name} Sir se baat kar raha hoon? "
            "Jai Mistry here from Craft Idea. Sir, kya ek minute baat kar sakte hain?")
    assert tokenize_address(line) == (
        "Hello, kya main {contact_name} {honorific} se baat kar raha hoon? "
        "Jai Mistry here from Craft Idea. {honorific}, kya ek minute baat kar sakte hain?")
    assert tokenize_address("Am I speaking with Mr. {contact_name}?") == \
        "Am I speaking with {title} {contact_name}?"


def test_tokenize_leaves_third_parties_and_lookalikes():
    assert tokenize_address("Mr. Umang says hi", protected={"umang"}) == "Mr. Umang says hi"
    assert tokenize_address("सर्विसेज के लिए धन्यवाद") == "सर्विसेज के लिए धन्यवाद"


def test_only_spoken_fields_are_tokenized():
    d = {
        "response_bank": {
            "opening_greeting": "Hello {contact_name} Sir",
            "closing_responses": {"hi": "Thank you Sir"},
            "objection_playbook": [{"patterns": ["no sir"], "answer": {"hi": "Ji Sir"}}],
        },
        "call_flow": {"stages": [{"id": "A", "goal": "Say verbatim: 'Thank you Sir.'"}]},
        "guardrails": {"never_rules": ["Never append 'Thank you Sir'"]},
        "knowledge_base": {"portfolio_prompt_text": "say: 'Sir, no numbers'"},
    }
    changed = tokenize_spoken_fields(d)
    assert d["response_bank"]["opening_greeting"] == "Hello {contact_name} {honorific}"
    assert d["response_bank"]["closing_responses"]["hi"] == "Thank you {honorific}"
    assert d["response_bank"]["objection_playbook"][0]["answer"]["hi"] == "Ji {honorific}"
    assert d["call_flow"]["stages"][0]["goal"] == "Say verbatim: 'Thank you {honorific}.'"
    # caller-side patterns, rules about the words, and knowledge prose: untouched
    assert d["response_bank"]["objection_playbook"][0]["patterns"] == ["no sir"]
    assert d["guardrails"]["never_rules"] == ["Never append 'Thank you Sir'"]
    assert d["knowledge_base"]["portfolio_prompt_text"] == "say: 'Sir, no numbers'"
    assert len(changed) == 4


def test_protected_names_come_from_titled_mentions():
    assert protected_names(["personal assistant to Mr. Umang, the CEO"]) == {"umang"}


def test_static_band_text_is_gender_neutral():
    assert neutral_address("Thank you {honorific}, see you {title} {contact_name}.") \
        == "Thank you, see you {contact_name}."


def test_a_quoted_script_line_is_rendered_for_this_caller_and_voice():
    goal = "Say verbatim: 'Thank you {honorific}. Main bas check karna chahta tha.'"
    out = render_author_line(goal, contact_name="Shahanaz", contact_gender="female",
                             agent_gender="female")
    assert out == "Say verbatim: 'Thank you Ma'am. Main bas check karna chahti thi.'"
    out = render_author_line(goal, contact_name="Shahanaz", contact_gender="",
                             agent_gender="male")
    assert out == "Say verbatim: 'Thank you. Main bas check karna chahta tha.'"


# ── the engine, end to end ─────────────────────────────────────────────────

JAI = "Hello, kya main {contact_name} Sir se baat kar raha hoon?"


def _router(speaker: str, *, name: str = "", gender: str = "",
            greeting: str = JAI, direction: str = "outbound",
            **over) -> ConversationRouter:
    agent = _agent({"hi": {"speaker": speaker}, "en": {"speaker": speaker}},
                   call_direction=direction,
                   response_bank={"opening_greeting": greeting, **over.pop("rb", {})},
                   **over)
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.set_call_context(direction=direction, contact_name=name,
                       contact_phone="9876543210" if name else "",
                       contact_gender=gender)
    return r


def _opening(r: ConversationRouter) -> str:
    return asyncio.run(r.generate_opening()).text


def test_the_jai_mistry_2_greeting_for_each_kind_of_contact():
    """The exact live line, simran voice, the three contacts on that account."""
    assert _opening(_router("simran", name="SHAHANAZ", gender="female")) == \
        "Hello, kya main SHAHANAZ Ma'am se baat kar rahi hoon?"
    assert _opening(_router("simran", name="JAY", gender="male")) == \
        "Hello, kya main JAY Sir se baat kar rahi hoon?"
    assert _opening(_router("simran", name="SHAHANAZ")) == \
        "Hello, kya main SHAHANAZ se baat kar rahi hoon?"


def test_a_male_voice_keeps_the_masculine_script():
    assert _opening(_router("shubh", name="SHREE", gender="female")) == \
        "Hello, kya main SHREE Ma'am se baat kar raha hoon?"


def test_no_name_opening_is_never_sir_maam():
    assert _opening(_router("simran")) == "Hello."


def test_an_opening_that_only_asked_who_they_are_falls_back_to_our_intro():
    out = _opening(_router("simran", greeting="Am I speaking to {contact_name}?"))
    assert out and "sir" not in out.lower() and "{" not in out
    assert "Asha" in out and "TestCo" in out


def test_every_streamed_reply_is_finalized():
    """A canned/playbook line with a literal Sir, spoken to a woman by a
    simran voice — through process_stream, the choke point."""
    from app.voice.conversation import EngineResult

    r = _router("simran", name="Shree", gender="female")

    async def _raw(_text):
        # the engine's own yields: a scripted line and a model chunk
        yield EngineResult(text="Sir, main aapko saari details bhej dunga.",
                           is_shortcut=True, intent="faq")
        yield EngineResult(text="Achha Sir, samajh gaya.", is_shortcut=False,
                           intent="general")
        yield EngineResult(text="Sir.", is_shortcut=False, intent="general")

    r._process_stream_raw = _raw

    async def _drive():
        return [x.text async for x in r.process_stream("price kya hai")]

    assert asyncio.run(_drive()) == [
        "Ma'am, main aapko saari details bhej dungi.",
        "Achha Ma'am, samajh gayi.",
        "Ma'am."]
    # no gender on file: the word goes, and a chunk left empty is DROPPED,
    # never handed to TTS as blank text
    r.outbound_contact_gender = ""
    assert asyncio.run(_drive()) == [
        "Main aapko saari details bhej dungi.",
        "Achha, samajh gayi."]


def test_history_records_what_was_said():
    r = _router("simran", name="Shree")
    r.memory.add_assistant("Thank you Sir, main samajh gaya.")
    assert r.memory.turns[-1].text == "Thank you, main samajh gayi."


def test_platform_canned_lines_follow_the_voice_too():
    """conversation.py's own hi/gu lines were written masculine and spoken by
    every female agent."""
    r = _router("simran", name="Shree")
    assert r._finalize_spoken("जी, समझ गया — मैं कैसे मदद कर सकता हूँ?") == \
        "जी, समझ गई — मैं कैसे मदद कर सकती हूँ?"


def test_the_stage_goal_reaches_the_prompt_rendered():
    goal = ("Say verbatim: 'Thank you {honorific}. Main bas ek quick sa check "
            "karna chahta tha — kya aap focus kar rahe hain?'")
    r = _router("simran", name="Shahanaz", gender="female",
                call_flow={"stages": [{"id": "S1", "order": 0, "goal": goal,
                                       "turns_until_advance": 8}]})
    prompt = r._build_system_prompt()
    assert "'Thank you Ma'am. Main bas ek quick sa check karna chahti thi" in prompt
    assert "{honorific}" not in prompt.split("━━ CURRENT GOAL ━━")[1][:400]


def test_prompt_tells_the_model_not_to_guess_for_an_unknown_contact():
    prompt = _router("simran", name="Shahanaz")._build_system_prompt()
    assert "NEVER call them Sir, Ma'am" in prompt
    assert "Your pronouns are she/her" in prompt


def test_prompt_pronouns_follow_a_male_voice_even_when_stored_she():
    r = _router("shubh", name="Rajesh", gender="male",
                identity={"agent_name": "Vishal Patel", "company_name": "Pramukh",
                          "pronouns": "she"},
                personality={"pronouns": "she"})
    prompt = r._build_system_prompt()
    assert "Your pronouns are he/him" in prompt
    assert "Your pronouns are she/her" not in prompt


# ── ingest: what a Gender column holds ─────────────────────────────────────

@pytest.mark.parametrize("cell,stored", [
    ("पुरुष", "male"), ("महिला", "female"), ("स्त्री", "female"),
    ("પુરુષ", "male"), ("સ્ત્રી", "female"), ("Mr", "male"), ("Mrs.", "female"),
    ("Gents", "male"), ("Ladies", "female"),
])
def test_indian_gender_values_normalize(cell, stored):
    from shared.contacts import normalize_gender
    assert normalize_gender(cell) == stored


def test_an_unrecognised_value_is_kept_but_never_addresses():
    from shared.contacts import normalize_gender
    assert normalize_gender("Prefer not to say") == "prefer not to say"
    assert enforce_address("Thank you Sir", normalize_gender("Prefer not to say")) \
        == "Thank you"


# ── Copilot: tokenize on write, remind about the data, default voice ───────

@pytest.fixture()
def cop(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    repo = Path(__file__).resolve().parents[3]
    for p in (str(repo / "dashboard"), str(repo)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import agent_copilot_api as cop
    return cop


@pytest.mark.parametrize("message,asks", [
    ("I want the agent to say Sir or Ma'am to customers", True),
    ("add sir/madam in the greeting", True),
    ("greeting mein सर lagao", True),
    ("Mr/Mrs before the name please", True),
    ("ok sir do it", False),                          # courtesy to US
    ("I WANT VOICE OG AGENT SHOULD BE IN FEMALE", False),
    ("change title of the agent", False),
])
def test_copilot_knows_when_the_user_asked_for_an_honorific(cop, message, asks):
    assert cop._asks_for_honorific(message) is asks


def test_copilot_reminder_names_the_data_and_never_offers_a_fixed_sir(cop):
    text = cop._gender_data_reminder("nobody@example.com")
    assert "Gender" in text and "never guesses" in text and "fixed Sir" in text


def test_copilot_tokenizes_the_change_it_writes_not_the_rules(cop):
    notes: list[str] = []
    d = cop._normalize_definition({
        "call_flow": {"stages": [{"id": "A", "goal":
                                  "Say verbatim: 'Thank you Sir, main samajh gaya.'"}]},
        "guardrails": {"never_rules": ["Never say 'Thank you Sir' twice"]}}, notes)
    assert d["call_flow"]["stages"][0]["goal"] == \
        "Say verbatim: 'Thank you {honorific}, main samajh gaya.'"
    assert d["guardrails"]["never_rules"] == ["Never say 'Thank you Sir' twice"]
    assert any("{honorific}" in n for n in notes)


def test_copilot_default_voice_follows_the_script_the_owner_wrote(cop):
    """The Kaapd brief's "करण बोल रहा हूँ" used to get priya."""
    male = cop._default_definition("kaapd", {"response_bank": {
        "opening_greeting": "नमस्ते जी! मैं कापड़, सूरत से करण बोल रहा हूँ।"}})
    from app.agent_definition.voice_catalog import speaker_gender
    assert {speaker_gender(v["speaker"]) for v in
            male["languages"]["tts_voices"].values()} == {"male"}
    neutral = cop._default_definition("x", {"response_bank": {
        "opening_greeting": "Hello! How can I help?"}})
    assert {speaker_gender(v["speaker"]) for v in
            neutral["languages"]["tts_voices"].values()} == {"female"}


# ── श्री <name> → <name> जी (call 1048) ────────────────────────────────────

@pytest.mark.parametrize("text,gender,name,expected", [
    ("श्री Vijay, बढ़िया — मैं बस", "male", "Vijay", "Vijay जी, बढ़िया — मैं बस"),
    ("समझ गया श्री Vijay, Pramukh में", "male", "Vijay", "समझ गया Vijay जी, Pramukh में"),
    ("ज़रूर श्री Vijay, रविवार को", "", "Vijay", "ज़रूर Vijay जी, रविवार को"),
    ("क्या मैं श्रीमती राजेश से बात कर रही हूँ?", "female", "राजेश",
     "क्या मैं राजेश जी से बात कर रही हूँ?"),
    ("क्या मैं श्री राजेश से बात कर रही हूँ?", "female", "राजेश",
     "क्या मैं राजेश जी से बात कर रही हूँ?"),
    ("Shri Rajesh ji, namaste", "female", "Rajesh", "Rajesh ji, namaste"),
    ("श्री Vijay Patel जी, नमस्ते", "male", "Vijay Patel", "Vijay Patel जी, नमस्ते"),
    ("શ્રી Vijay, કેમ છો?", "male", "Vijay", "Vijay જી, કેમ છો?"),
])
def test_shri_before_the_callers_name_becomes_ji_after_it(text, gender, name, expected):
    out = enforce_address(text, gender, name)
    assert out == expected
    assert enforce_address(out, gender, name) == out


def test_a_third_partys_shri_is_left_alone():
    out = enforce_address("Pramukh Group, founded by Shri Natubhai R. Bhatu.",
                          "male", "Vijay")
    assert out == "Pramukh Group, founded by Shri Natubhai R. Bhatu."


def test_the_prompt_tells_the_model_ji_not_shri():
    from app.voice.prompt_blocks import _address_lines
    text = " ".join(_address_lines("सर", "श्री", "Vijay Kumar"))
    assert "'Vijay जी'" in text
    assert "'श्री' before their name" not in text
    assert "Mr." in " ".join(_address_lines("Sir", "Mr.", "Raj"))
