"""
F1 — prompt_blocks.objection_block scripted-answer rendering.

Pins today's behaviour:
- entries with prompt_guidance → guided branch (guidance lines only,
  {trust_proofs} substituted; scripted answers of other entries dropped);
- entries with id/patterns/answer but NO prompt_guidance → one composed line
  per entry: "'p1' / 'p2' → answer" (first 2 patterns; id fallback), answer
  chain lang→en→first, {trust_proofs} substituted, no-answer entries skipped,
  at most 8 lines, whitespace collapsed, ~240-char cap with '...' suffix;
- EMPTY playbook → the legacy 4 hardcoded real-estate lines byte-exact;
- en-only answers render for lang='hi'.

No network, no LLM — pure prompt_blocks functions.
"""
from __future__ import annotations

from app.agent_definition.schema import AgentDefinition
from app.voice import prompt_blocks as pb

HEADER = "━━ OBJECTION HANDLING ━━"


def make_agent(**over) -> AgentDefinition:
    d = {
        "agent_id": "t_obj",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en",
                      "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
        ]},
        "response_bank": {"opening_greeting": "hi"},
    }
    d.update(over)
    return AgentDefinition.from_dict(d)


def playbook_agent(playbook, trust_proofs=None):
    over = {"response_bank": {"opening_greeting": "hi",
                              "objection_playbook": playbook}}
    if trust_proofs is not None:
        over["brand"] = {"trust_proofs": trust_proofs}
    return make_agent(**over)


# ── (a) guided branch ───────────────────────────────────────────────────────

def test_guided_branch_wins_and_substitutes_trust_proofs():
    agent = playbook_agent(
        [
            {"id": "price", "patterns": ["too costly"],
             "answer": {"en": "scripted answer must not render"},
             "prompt_guidance": "Handle price doubts with {trust_proofs}."},
            {"id": "brochure", "patterns": ["send brochure"],
             "answer": {"en": "another scripted answer"}},
        ],
        trust_proofs=["RERA registered", "500 families"],
    )
    band = pb.objection_block(agent, lang="en")
    assert band[:2] == ["", HEADER]
    assert band[2:] == ["Handle price doubts with RERA registered; 500 families."]
    joined = "\n".join(band)
    # guided branch takes the whole band — scripted answers are NOT composed
    assert "scripted answer must not render" not in joined
    assert "another scripted answer" not in joined


# ── (b) scripted answers — composed lines ───────────────────────────────────

def test_scripted_entries_render_one_line_each_with_pattern_cap():
    agent = playbook_agent([
        {"id": "price_high",
         "patterns": ["too costly", "expensive", "third pattern ignored"],
         "answer": {"en": "Quality lasts longer."}},
        {"id": "need_time",  # no patterns → humanized id becomes the trigger
         "answer": {"en": "Take your time."}},
    ])
    band = pb.objection_block(agent, lang="en")
    assert band[:2] == ["", HEADER]
    assert band[2] == "'too costly' / 'expensive' → Quality lasts longer."
    assert band[3] == "'need time' → Take your time."
    assert len(band) == 4


def test_scripted_answer_trust_proofs_substituted():
    agent = playbook_agent(
        [{"id": "doubt", "patterns": ["not sure"],
          "answer": {"en": "Remember: {trust_proofs}."}}],
        trust_proofs=["30 years in business", "zero complaints"],
    )
    band = pb.objection_block(agent, lang="en")
    assert band[2] == "'not sure' → Remember: 30 years in business; zero complaints."


def test_entries_without_answer_are_skipped():
    agent = playbook_agent([
        {"id": "no_answer_entry", "patterns": ["hmm"]},
        {"id": "empty_answer", "patterns": ["uh"], "answer": {}},
        {"id": "real", "patterns": ["costly"], "answer": {"en": "Worth it."}},
    ])
    band = pb.objection_block(agent, lang="en")
    assert band == ["", HEADER, "'costly' → Worth it."]


def test_at_most_8_composed_lines():
    playbook = [{"id": f"obj_{i}", "patterns": [f"pattern {i}"],
                 "answer": {"en": f"answer {i}"}} for i in range(12)]
    agent = playbook_agent(playbook)
    band = pb.objection_block(agent, lang="en")
    assert len(band) == 2 + 8
    assert band[-1] == "'pattern 7' → answer 7"


def test_answer_whitespace_collapsed_and_capped_240():
    long_answer = ("word  \n" * 60).strip()  # newlines + double spaces, >240
    agent = playbook_agent([
        {"id": "long", "patterns": ["ramble"], "answer": {"en": long_answer}},
    ])
    band = pb.objection_block(agent, lang="en")
    line = band[2]
    answer_part = line.split(" → ", 1)[1]
    assert "\n" not in answer_part and "  " not in answer_part
    assert answer_part.endswith("...")
    assert len(answer_part) <= 240
    assert answer_part.startswith("word word word")


# ── (c) empty playbook → legacy band byte-exact ─────────────────────────────

def test_empty_playbook_renders_legacy_four_lines_byte_exact():
    agent = make_agent()
    band = pb.objection_block(agent, lang="en")
    assert band == [
        "",
        "━━ OBJECTION HANDLING ━━",
        "'Send brochure' → agree to send AND invite visit — photos tell only part of the story.",
        "'Price too high' → ask their comfortable range, suggest lower options.",
        "'Need to ask family' → invite the whole family to visit together.",
        "'Comparing builders' → smart — but see ours first. 30 years, zero stalls, RERA.",
    ]
    assert ("'Comparing builders' → smart — but see ours first. "
            "30 years, zero stalls, RERA.") in band


# ── (d) lang chain: en-only answer renders for hi ───────────────────────────

def test_en_only_answer_renders_for_hi_lang():
    agent = playbook_agent([
        {"id": "price", "patterns": ["mehenga"],
         "answer": {"en": "English fallback answer."}},
    ])
    band = pb.objection_block(agent, lang="hi")
    assert band[2] == "'mehenga' → English fallback answer."
