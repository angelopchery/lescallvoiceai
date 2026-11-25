"""{honorific} — gendered address from the contact sheet (2026-09-10).

`contacts.gender` had been ingested since the phone book existed — a "Gender"
or "Sex" column in an upload is mapped to it, the public API accepts it, the
Contacts sheet of every export prints it — and nothing downstream ever read it.
This suite covers the chain that finally does, and, more importantly, covers
the OPTIONALITY that makes it safe to put in a shared greeting:

    a contact with a gender is addressed as Sir / Ma'am and Mr. / Mrs.,
    everywhere in the agent and not only in the greeting;
    a contact without one is addressed exactly as they were before,
    with the token and the gap it leaves removed from the spoken line.

The second property is the load-bearing one. Gender is an optional column in
an optional upload, so the ABSENT case is the common case, and a greeting that
degraded into "Good morning , am I speaking with Rajesh?" — or worse, guessed —
would be a regression for every tenant whose sheet never had the column.
"""

from __future__ import annotations

import pytest

from app.voice import prompt_blocks as pb
from app.voice.conversation import (
    HONORIFIC_PREVIEW_GENDER, _honorific_word, _tidy_removed_token_seams,
    _title_word)
from test_call_termination import make_router


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


# ── the word itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("gender,expected", [
    ("male", "Sir"),
    ("female", "Ma'am"),
    ("MALE", "Sir"),          # set_call_context lowercases, but be defensive
    ("  female ", "Ma'am"),
])
def test_known_gender_resolves_to_a_title(gender, expected):
    assert _honorific_word(gender) == expected


@pytest.mark.parametrize("gender", [
    "", None, "unknown", "prefer not to say", "other", "n/a", "-",
    "m", "f",                 # raw sheet values; normalize_gender maps these
                              # at INGEST, so anything reaching here unmapped
                              # is not a gender we can act on
])
def test_unknown_gender_resolves_to_nothing(gender):
    """Never guess. A wrong honorific is worse than none at all."""
    assert _honorific_word(gender) == ""


def test_language_is_explicit_when_the_caller_knows_it():
    assert _honorific_word("male", lang="hi") == "सर"
    assert _honorific_word("female", lang="hi") == "मैडम"
    assert _honorific_word("male", lang="gu") == "સર"
    assert _honorific_word("female", lang="gu") == "મેડમ"


def test_language_falls_back_to_the_script_of_the_line():
    """A spoken line has no language field — only its own characters. An
    English "Sir" inside a Devanagari greeting makes the TTS code-switch
    mid-sentence, the same failure the time-of-day word guards against."""
    assert _honorific_word("male", "नमस्ते, आप कैसे हैं?") == "सर"
    assert _honorific_word("female", "કેમ છો?") == "મેડમ"
    assert _honorific_word("male", "Good morning!") == "Sir"


def test_unrecognised_language_code_falls_back_to_english():
    assert _honorific_word("male", lang="ta") == "Sir"


# ── substitution in a spoken line ───────────────────────────────────────────

def personalized(text: str, gender: str = "", lang: str = "en") -> str:
    r = make_router()
    r.outbound_contact_gender = gender
    r.outbound_contact_name = "Rajesh"
    return r._personalize(text, "response_bank.opening_greeting")


GREETING = "Good {current_time} {honorific}, am I speaking with {contact_name}?"


def test_greeting_with_gender_speaks_the_title():
    out = personalized(GREETING, "male")
    assert " Sir, am I speaking with Rajesh?" in out
    assert "{" not in out


def test_greeting_without_gender_reads_as_if_the_token_were_never_there():
    """THE contract. The same authored line has to work for a contact list
    that carries gender and one that does not."""
    out = personalized(GREETING, "")
    assert "am I speaking with Rajesh?" in out
    assert "Sir" not in out and "Ma'am" not in out
    # no orphaned space before the comma, no double space, no stray brace
    assert " ," not in out and "  " not in out and "{" not in out
    assert out.startswith("Good ")


def test_female_contact_is_never_called_sir():
    assert "Ma'am" in personalized(GREETING, "female")
    assert "Sir" not in personalized(GREETING, "female")


@pytest.mark.parametrize("token", [
    "{honorific}", "{{honorific}}", "${honorific}", "[honorific]",
    "{Sir/Ma'am}", "{sir_maam}", "{sir or ma'am}", "{salutation}",
    "{gender_honorific}", "{ honorific }",
])
def test_invented_syntaxes_are_tolerated(token):
    """LLM editors reword placeholders constantly; every shape an author or
    the copilot might produce has to resolve, not reach TTS."""
    out = personalized(f"Hello {token}, how are you?", "male")
    assert out == "Hello Sir, how are you?"


@pytest.mark.parametrize("token", ["{gender}", "{sex}"])
def test_the_raw_column_is_never_spoken(token):
    """{gender} reads as "insert the column value", which would say the word
    "male" to the caller. It is deliberately NOT an honorific token — it is
    stripped by the unresolved-token backstop instead."""
    out = personalized(f"Hello {token}, how are you?", "male")
    assert "male" not in out.lower()
    assert "{" not in out
    assert out == "Hello, how are you?"


def test_honorific_localizes_inside_a_hindi_greeting():
    out = personalized("नमस्ते {honorific}, कैसे हैं आप?", "female")
    assert "मैडम" in out and "Ma'am" not in out


def test_seam_tidier_closes_the_gap_a_removed_token_leaves():
    assert _tidy_removed_token_seams("Good morning , am I") == "Good morning, am I"
    assert _tidy_removed_token_seams("the guardian of ?") == "the guardian?"
    assert _tidy_removed_token_seams("a  b") == "a b"


# ── the call-context handoff ────────────────────────────────────────────────

def test_set_call_context_carries_gender():
    r = make_router()
    r.set_call_context(direction="outbound", contact_name="Rajesh",
                       contact_phone="9876543210", contact_gender="Male")
    assert r.outbound_contact_gender == "male"


def test_set_call_context_without_gender_leaves_it_empty():
    r = make_router()
    r.set_call_context(direction="outbound", contact_name="Rajesh")
    assert r.outbound_contact_gender == ""


def test_customer_context_supplies_gender_on_an_inbound_call():
    """An inbound call has no dial record, so the phone-book match on the
    caller id is the only place an honorific can come from."""
    r = make_router()
    r.set_customer_context({"name": "Rajesh", "gender": "male",
                            "calls_count": 3, "digest": "a returning buyer"})
    assert r.outbound_contact_gender == "male"


def test_customer_context_never_overrides_the_verified_dial_identity():
    """The dial record is THIS leg's verified identity (RUNBOOK §33); a
    phone-book row matched on caller id must not talk over it."""
    r = make_router()
    r.set_call_context(direction="outbound", contact_name="Rajesh",
                       contact_gender="female")
    r.set_customer_context({"name": "Rajesh", "gender": "male",
                            "calls_count": 3, "digest": "x"})
    assert r.outbound_contact_gender == "female"


def test_customer_context_without_gender_is_harmless():
    r = make_router()
    r.set_customer_context({"name": "Rajesh", "calls_count": 2, "digest": "x"})
    assert r.outbound_contact_gender == ""


# ── the prompt band ─────────────────────────────────────────────────────────

def _direction_lines(honorific: str, direction: str = "outbound",
                     name_title: str = "") -> str:
    agent = make_router()._def
    return "\n".join(pb.direction_block(
        agent, direction, "Rajesh", "9876543210", lang="en",
        honorific=honorific, name_title=name_title))


def test_prompt_tells_the_model_how_to_address_a_known_contact():
    out = _direction_lines("Sir")
    assert "HOW TO ADDRESS THEM" in out and "'Sir'" in out


def test_prompt_forbids_gendered_address_when_gender_is_unknown():
    """RUNBOOK §42 reversed the 09-10 contract. Saying nothing left the model
    to fill the silence itself — "Mrs. Paul" then "Mr. Paul" to one caller —
    so an unknown gender now gets an explicit instruction NOT to guess."""
    out = _direction_lines("")
    assert "HOW TO ADDRESS THEM" in out
    assert "NEVER call them Sir, Ma'am" in out
    assert "use 'Sir'" not in out and "use 'Ma'am'" not in out


def test_prompt_never_leaks_the_raw_gender_to_the_model():
    """direction_block takes the resolved WORD, not the column value, so the
    model cannot repeat "male" back to the caller."""
    out = _direction_lines("Ma'am")
    assert "female" not in out and "male" not in out


def test_inbound_known_caller_also_gets_an_address_line():
    out = _direction_lines("Sir", direction="inbound")
    assert "'Sir'" in out
    assert "INBOUND" in out


def test_inbound_unknown_caller_is_told_not_to_guess():
    """An inbound caller with no phone-book match has no gender on file —
    the model must not supply one."""
    out = _direction_lines("", direction="inbound")
    assert "INBOUND" in out and "NEVER call them" in out
    assert "use 'Sir'" not in out


# ── preview resolution ──────────────────────────────────────────────────────

def test_preview_keeps_the_token_visible():
    """A preview stands for EVERY contact, including the ones that do carry a
    gender. Resolving it away would tell an author their placeholder is
    broken; the sentinel is swapped back to {honorific} by spoken_surfaces."""
    r = make_router()
    r.outbound_contact_gender = HONORIFIC_PREVIEW_GENDER
    out = r._personalize("Good morning {honorific}, {title} Rajesh!", "x")
    assert out != "Good morning, Rajesh!"
    assert "{" not in out          # sentinel words, not braces, reach TTS
    from app.voice.conversation import (
        HONORIFIC_PREVIEW_WORD, TITLE_PREVIEW_WORD)
    assert HONORIFIC_PREVIEW_WORD in out and TITLE_PREVIEW_WORD in out


# ── {title}: the name-prefix half ───────────────────────────────────────────
#
# "Sir" stands alone; "Mr." only ever precedes a name. Two tokens, because an
# author needs both in different slots of the same sentence.

@pytest.mark.parametrize("gender,expected", [
    ("male", "Mr."), ("female", "Mrs."), ("MALE", "Mr."),
])
def test_known_gender_resolves_to_a_name_prefix(gender, expected):
    assert _title_word(gender) == expected


@pytest.mark.parametrize("gender", ["", "unknown", "prefer not to say", "m"])
def test_unknown_gender_yields_no_name_prefix(gender):
    assert _title_word(gender) == ""


def test_name_prefix_localizes():
    assert _title_word("male", lang="hi") == "श्री"
    assert _title_word("female", lang="hi") == "श्रीमती"
    assert _title_word("male", lang="gu") == "શ્રી"
    assert _title_word("female", lang="gu") == "શ્રીમતી"
    assert _title_word("female", "नमस्ते, कैसे हैं?") == "श्रीमती"


NAMED = "Am I speaking with {title} {contact_name}?"


def test_title_sits_in_front_of_the_name():
    assert personalized(NAMED, "male") == "Am I speaking with Mr. Rajesh?"
    assert personalized(NAMED, "female") == "Am I speaking with Mrs. Rajesh?"


def test_title_disappears_cleanly_without_a_gender():
    """Same contract as {honorific}: no gender, no prefix, no double space."""
    out = personalized(NAMED, "")
    assert out == "Am I speaking with Rajesh?"
    assert "  " not in out and "{" not in out


def test_both_tokens_in_one_line():
    line = "Good morning {honorific}, am I speaking with {title} {contact_name}?"
    assert personalized(line, "male") == (
        "Good morning Sir, am I speaking with Mr. Rajesh?")
    assert personalized(line, "") == (
        "Good morning, am I speaking with Rajesh?")


@pytest.mark.parametrize("token", [
    "{title}", "{{title}}", "${title}", "[title]", "{Mr/Mrs}", "{mr_mrs}",
    "{name_title}", "{name_prefix}", "{prefix}", "{Mr./Mrs.}", "{gender_title}",
])
def test_title_syntax_variants_are_tolerated(token):
    assert personalized(f"Hello {token} Rajesh.", "male") == "Hello Mr. Rajesh."


def test_the_two_gendered_tokens_never_collide():
    """A line asking for both must not get the same word twice — the whole
    reason they are separate tokens."""
    out = personalized("{honorific}! Nice to meet you, {title} Rajesh.", "female")
    assert out == "Ma'am! Nice to meet you, Mrs. Rajesh."


# ── "all such things in the agent", not just the greeting ───────────────────
#
# Every authored spoken line funnels through _lang_response — closings, canned
# phrases, objection answers, identity responses, graceful close, wrong-number
# lines, timeout fallbacks. Substitution lives there so gendered address is a
# property of the AGENT, not a property of the greeting field.

def _router_with(gender: str, **bank):
    r = make_router(response_bank={"opening_greeting": "hi", **bank})
    r.set_call_context(direction="outbound", contact_name="Rajesh",
                       contact_gender=gender)
    return r


def test_closing_line_is_personalized_too():
    r = _router_with("male", closing_responses={
        "en": "Thank you for your time {honorific}. Have a great day!"})
    assert r._lang_response(r._def.response_bank.closing_responses) == (
        "Thank you for your time Sir. Have a great day!")


def test_closing_line_degrades_without_a_gender():
    r = _router_with("", closing_responses={
        "en": "Thank you for your time {honorific}. Have a great day!"})
    assert r._lang_response(r._def.response_bank.closing_responses) == (
        "Thank you for your time. Have a great day!")


def test_identity_response_is_personalized_too():
    r = _router_with("female", identity_responses={
        "en": "Yes {honorific}, this is Asha."})
    assert r._lang_response(r._def.response_bank.identity_responses) == (
        "Yes Ma'am, this is Asha.")


def test_a_rotating_list_response_is_personalized_on_every_pick():
    r = _router_with("male", objection_responses={
        "en": ["One moment {honorific}.", "Of course {honorific}."]})
    bank = r._def.response_bank.objection_responses
    picks = {r._lang_response(bank) for _ in range(4)}
    assert picks == {"One moment Sir.", "Of course Sir."}


def test_lang_response_does_not_eat_tokens_it_does_not_own():
    """{trust_proofs} is resolved by _match_objection_playbook AFTER this
    returns. Running the strip backstop inside _lang_response would delete it
    — which is why substitution and stripping are separate."""
    r = _router_with("male")
    assert r._lang_response({"en": "We are {trust_proofs} {honorific}."}) == (
        "We are {trust_proofs} Sir.")


def test_objection_playbook_answer_keeps_trust_proofs_and_gains_a_title():
    r = make_router(
        brand={"trust_proofs": ["RERA registered"]},
        response_bank={"opening_greeting": "hi", "objection_playbook": [
            {"id": "o1", "patterns": ["who are you"],
             "answer": {"en": "{honorific}, we are {trust_proofs}."}}]})
    r.set_call_context(direction="outbound", contact_name="Rajesh",
                       contact_gender="male")
    assert r._match_objection_playbook("who are you") == (
        "Sir, we are RERA registered.")


# ── the prompt band teaches the model BOTH forms ────────────────────────────

def test_prompt_gives_the_model_both_address_forms():
    out = _direction_lines("Sir", name_title="Mr.")
    assert "HOW TO ADDRESS THEM" in out
    assert "'Sir'" in out and "'Mr.'" in out
    assert "Mr. Rajesh" in out              # worked example uses the real name
    assert "WHOLE call" in out              # not just the greeting


def test_prompt_address_line_forbids_both_forms_without_a_gender():
    out = _direction_lines("", name_title="")
    assert "NEVER call them" in out and "Mr., Mrs." in out
    assert "'Mr.' before their name" not in out


def test_prompt_never_leaks_the_raw_gender_with_titles_on():
    out = _direction_lines("Ma'am", name_title="Mrs.")
    assert "female" not in out and "male" not in out


# ── the ingest half: a Gender column in an uploaded sheet ───────────────────
#
# These import the real dashboard module (as test_outbound_contacts_api.py
# does) rather than re-implementing the parser, because the point is that the
# column an operator actually uploads reaches contacts.gender in the two
# values the honorific resolver recognises.

import sys                                                    # noqa: E402
from pathlib import Path                                      # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
for _p in (str(_REPO / "dashboard"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import server  # noqa: E402


@pytest.mark.parametrize("header,expected_idx", [
    (["Name", "Mobile", "Gender", "City"], 2),
    (["Full Name", "Phone Number", "Sex"], 2),
    (["name", "phone", "GENDER"], 2),
    (["Name", "Phone", "City"], None),          # the common sheet: no column
])
def test_a_gender_column_is_detected_by_header(header, expected_idx):
    assert server._map_header_columns(header)["gender"] == expected_idx


@pytest.mark.parametrize("cell,stored", [
    ("M", "male"), ("male", "male"), ("Male", "male"), ("MAN", "male"),
    ("F", "female"), ("female", "female"), ("Woman", "female"),
])
def test_sheet_values_normalize_to_what_the_resolver_reads(cell, stored):
    """The two halves have to agree: whatever the spreadsheet author typed is
    normalized at INGEST, and the resolver only ever sees male/female."""
    assert server._normalize_gender(cell) == stored
    assert _honorific_word(stored) in ("Sir", "Ma'am")


@pytest.mark.parametrize("cell", ["", "   ", "Prefer not to say", "N/A", "?"])
def test_unrecognised_sheet_values_never_become_an_honorific(cell):
    assert _honorific_word(server._normalize_gender(cell)) == ""


def test_row_with_a_gender_column_carries_it_through():
    cm = server._map_header_columns(["Name", "Mobile", "Gender", "City"])
    row = server._row_to_contact_mapped(
        ["Rajesh Patel", "9876543210", "M", "Surat"], cm)
    assert row["gender"] == "male"
    # every unmapped column still lands in details — gender did not eat one
    assert row["details"] == {"City": "Surat"}


def test_row_without_a_gender_column_is_unchanged():
    cm = server._map_header_columns(["Name", "Phone", "City"])
    row = server._row_to_contact_mapped(
        ["Rajesh Patel", "9876543210", "Surat"], cm)
    assert row["gender"] == ""


def test_gender_is_a_column_on_the_exported_contacts_sheet():
    """The round trip an operator sees: a gender uploaded in a sheet comes
    back out in one, so they can audit who will be addressed by title."""
    from shared.intelligence.selection_export import CONTACT_COLUMNS
    assert ("Gender", "gender") in CONTACT_COLUMNS
